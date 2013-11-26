# -*- coding: utf-8 -*-
#
#
# TheVirtualBrain-Framework Package. This package holds all Data Management, and 
# Web-UI helpful to run brain-simulations. To use it, you also need do download
# TheVirtualBrain-Scientific Package (for simulators). See content of the
# documentation-folder for more details. See also http://www.thevirtualbrain.org
#
# (c) 2012-2013, Baycrest Centre for Geriatric Care ("Baycrest")
#
# This program is free software; you can redistribute it and/or modify it under 
# the terms of the GNU General Public License version 2 as published by the Free
# Software Foundation. This program is distributed in the hope that it will be
# useful, but WITHOUT ANY WARRANTY; without even the implied warranty of 
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public
# License for more details. You should have received a copy of the GNU General 
# Public License along with this program; if not, you can download it here
# http://www.gnu.org/licenses/old-licenses/gpl-2.0
#
#
#   CITATION:
# When using The Virtual Brain for scientific publications, please cite it as follows:
#
#   Paula Sanz Leon, Stuart A. Knock, M. Marmaduke Woodman, Lia Domide,
#   Jochen Mersmann, Anthony R. McIntosh, Viktor Jirsa (2013)
#       The Virtual Brain: a simulator of primate brain network dynamics.
#   Frontiers in Neuroinformatics (7:10. doi: 10.3389/fninf.2013.00010)
#
#

"""
.. moduleauthor:: Lia Domide <lia.domide@codemart.ro>
.. moduleauthor:: Calin Pavel <calin.pavel@codemart.ro>
.. moduleauthor:: Bogdan Neacsa <bogdan.neacsa@codemart.ro>
"""

import os
import json
import shutil
from tvb.config import ADAPTERS
from cgi import FieldStorage
from datetime import datetime
from cherrypy._cpreqbody import Part
from sqlalchemy.orm.attributes import manager_of_class
from sqlalchemy.exc import IntegrityError
from tvb.basic.config.settings import TVBSettings as cfg
from tvb.basic.logger.builder import get_logger
from tvb.core.entities import model
from tvb.core.entities.storage import dao, transactional
from tvb.core.entities.model.model_burst import BURST_INFO_FILE, BURSTS_DICT_KEY, DT_BURST_MAP
from tvb.core.services.exceptions import ProjectImportException
from tvb.core.services.project_service import ProjectService
from tvb.core.entities.file.xml_metadata_handlers import XMLReader
from tvb.core.entities.file.files_helper import FilesHelper
from tvb.core.entities.file.hdf5_storage_manager import HDF5StorageManager
from tvb.core.entities.file.files_update_manager import FilesUpdateManager
from tvb.core.entities.file.exceptions import FileStructureException, MissingDataSetException
from tvb.core.entities.transient.burst_export_entities import BurstInformation
from tvb.core.entities.transient.structure_entities import DataTypeMetaData



class ImportService():
    """
    Service for importing TVB entities into system.
    It supports TVB exported H5 files as input, but it should also handle H5 files 
    generated outside of TVB, as long as they respect the same structure.
    """


    def __init__(self):
        self.logger = get_logger(__name__)
        self.user_id = None
        self.files_helper = FilesHelper()
        self.created_projects = []


    @transactional
    def import_project_structure(self, uploaded, user_id):
        """
        Execute import operations:
         
        1. check if ZIP or folder 
        2. find all project nodes
        3. for each project node:
            - create project
            - create all operations
            - import all images
            - create all dataTypes
        """

        self.user_id = user_id
        self.created_projects = []

        # Now we compute the name of the file where to store uploaded project
        now = datetime.now()
        date_str = "%d-%d-%d_%d-%d-%d_%d" % (now.year, now.month, now.day, now.hour,
                                             now.minute, now.second, now.microsecond)
        uq_name = "%s-ImportProject" % date_str
        uq_file_name = os.path.join(cfg.TVB_TEMP_FOLDER, uq_name + ".zip")

        temp_folder = None
        try:
            if isinstance(uploaded, FieldStorage) or isinstance(uploaded, Part):
                if uploaded.file:
                    file_obj = open(uq_file_name, 'wb')
                    file_obj.write(uploaded.file.read())
                    file_obj.close()
                else:
                    raise ProjectImportException("Please select the archive which contains the project structure.")
            else:
                shutil.copyfile(uploaded, uq_file_name)

            # Now compute the name of the folder where to explode uploaded ZIP file
            temp_folder = os.path.join(cfg.TVB_TEMP_FOLDER, uq_name)
            try:
                self.files_helper.unpack_zip(uq_file_name, temp_folder)
            except FileStructureException, excep:
                self.logger.exception(excep)
                raise ProjectImportException("Bad ZIP archive provided. A TVB exported project is expected!")

            try:
                self._import_project_from_folder(temp_folder)
            except Exception, excep:
                self.logger.exception(excep)
                self.logger.debug("Error encountered during import. Deleting projects created during this operation.")

                # Roll back projects created so far
                project_service = ProjectService()
                for project in self.created_projects:
                    project_service.remove_project(project.id)

                raise ProjectImportException(str(excep))

        finally:
            # Now delete uploaded file
            if os.path.exists(uq_file_name):
                os.remove(uq_file_name)
            # Now delete temporary folder where uploaded ZIP was exploded.
            if temp_folder is not None and os.path.exists(temp_folder):
                shutil.rmtree(temp_folder)


    def _import_project_from_folder(self, temp_folder):
        """
        Process each project from the uploaded pack, to extract names.
        """
        project_roots = []
        for root, _, files in os.walk(temp_folder):
            if FilesHelper.TVB_PROJECT_FILE in files:
                project_roots.append(root)

        for project_path in project_roots:
            project_entity = self.__populate_project(project_path)

            bursts_dict = {}
            dt_mappings_dict = {}
            bursts_file = os.path.join(project_path, BURST_INFO_FILE)
            if os.path.isfile(bursts_file):
                bursts_info_dict = json.loads(open(bursts_file, 'r').read())
                bursts_dict = bursts_info_dict[BURSTS_DICT_KEY]
                dt_mappings_dict = bursts_info_dict[DT_BURST_MAP]

            # Compute the path where to store files of the imported project
            new_project_path = os.path.join(cfg.TVB_STORAGE, FilesHelper.PROJECTS_FOLDER, project_entity.name)
            if project_path != new_project_path:
                shutil.copytree(project_path, new_project_path)
                shutil.rmtree(project_path)

            self.created_projects.append(project_entity)
            # Re-create old bursts, but keep a mapping between the id it has here and the old-id it had
            # in the project where they were exported, so we can re-add the datatypes to them.
            burst_ids_mapping = {}
            # Keep a list with all burst that were imported since we will want to also add the workflow
            # steps after we are finished with importing the operations and datatypes. We need to first
            # stored bursts since we need to know which new id's they have for operations parent_burst.
            if bursts_dict:
                for old_burst_id in bursts_dict:
                    burst_information = BurstInformation.load_from_dict(bursts_dict[old_burst_id])
                    burst_entity = model.BurstConfiguration(project_entity.id)
                    burst_entity.from_dict(burst_information.data)
                    burst_entity = dao.store_entity(burst_entity)
                    burst_ids_mapping[int(old_burst_id)] = burst_entity.id
                    # We don't need the data in dictionary form anymore, so update it with new BurstInformation object
                    bursts_dict[old_burst_id] = burst_information
            # Now import project operations
            self.import_project_operations(project_entity, new_project_path, dt_mappings_dict, burst_ids_mapping)
            # Now we can finally import workflow related entities
            self.import_workflows(project_entity, bursts_dict, burst_ids_mapping)


    def import_workflows(self, project, bursts_dict, burst_ids_mapping):
        """
        Import the workflow entities for all bursts imported in the project.

        :param project: the current

        :param bursts_dict: a dictionary that holds all the required information in order to
                            import the bursts from the new project

        :param burst_ids_mapping: a dictionary of the form {old_burst_id : new_burst_id} so we
                                  know what burst to link each workflow to
        """
        for burst_id in bursts_dict:
            workflows_info = bursts_dict[burst_id].get_workflows()
            for one_wf_info in workflows_info:
                # Use the new burst id when creating the workflow
                workflow_entity = model.Workflow(project.id, burst_ids_mapping[int(burst_id)])
                workflow_entity.from_dict(one_wf_info.data)
                workflow_entity = dao.store_entity(workflow_entity)
                wf_steps_info = one_wf_info.get_workflow_steps()
                view_steps_info = one_wf_info.get_view_steps()
                self.import_workflow_steps(workflow_entity, wf_steps_info, view_steps_info)


    def import_workflow_steps(self, workflow, wf_steps, view_steps):
        """
        Import all workflow steps for the given workflow. We create both wf_steps and view_steps
        in the same method, since if a wf_step has to be omited for some reason, we also need to
        omit that view step.
        :param workflow: a model.Workflow entity from which we need to add workflow steps

        :param wf_steps: a list of WorkflowStepInformation entities, from which we will rebuild 
                         the workflow steps

        :param view_steps: a list of WorkflowViewStepInformation entities, from which we will 
                           rebuild the workflow view steps

        """
        for wf_step in wf_steps:
            algorithm = wf_step.get_algorithm()
            if algorithm is None:
                # The algorithm is invalid for some reason. Just remove also the view step.
                position = wf_step.index()
                for entry in view_steps:
                    if entry.index() == position:
                        view_steps.remove(entry)
                continue
            wf_step_entity = model.WorkflowStep(algorithm.id)
            wf_step_entity.from_dict(wf_step.data)
            wf_step_entity.fk_workflow = workflow.id
            wf_step_entity.fk_operation = wf_step.get_operagion().id
            dao.store_entity(wf_step_entity)
        for view_step in view_steps:
            algorithm = view_step.get_algorithm()
            if algorithm is None:
                continue
            view_step_entity = model.WorkflowStepView(algorithm.id)
            view_step_entity.from_dict(view_step.data)
            view_step_entity.fk_workflow = workflow.id
            view_step_entity.fk_portlet = view_step.get_portlet().id
            dao.store_entity(view_step_entity)


    def import_project_operations(self, project, import_path, dt_burst_mappings=None, burst_ids_mapping=None):
        """
        This method scans provided folder and identify all operations that needs to be imported
        """
        if burst_ids_mapping is None:
            burst_ids_mapping = {}
        if dt_burst_mappings is None:
            dt_burst_mappings = {}
        # Identify folders containing operations
        operations = []
        for root, _, files in os.walk(import_path):
            if FilesHelper.TVB_OPERARATION_FILE in files:
                # Found an operation folder - append TMP to its name
                tmp_op_folder = os.path.join(os.path.split(root)[0], os.path.split(root)[1] + 'tmp')
                os.rename(root, tmp_op_folder)

                operation_file_path = os.path.join(tmp_op_folder, FilesHelper.TVB_OPERARATION_FILE)
                operation = self.__build_operation_from_file(project, operation_file_path)
                operation.import_file = operation_file_path
                operations.append(operation)

        imported_operations = []

        # Now we sort operations by start date, to be sure data dependency is resolved correct
        operations = sorted(operations, key=lambda operation: (operation.start_date or
                                                               operation.create_date or datetime.now()))

        # Here we process each operation found
        for operation in operations:
            old_operation_folder, _ = os.path.split(operation.import_file)
            operation_entity, datatype_group = self.__import_operation(operation)

            # Rename operation folder with the ID of the stored operation 
            new_operation_path = FilesHelper().get_operation_folder(project.name, operation_entity.id)
            if old_operation_folder != new_operation_path:
                # Delete folder of the new operation, otherwise move will fail
                shutil.rmtree(new_operation_path)
                shutil.move(old_operation_folder, new_operation_path)

            # Now process data types for each operation
            all_datatypes = []
            for file_name in os.listdir(new_operation_path):
                if file_name.endswith(FilesHelper.TVB_STORAGE_FILE_EXTENSION):
                    file_update_manager = FilesUpdateManager()
                    file_update_manager.upgrade_file(os.path.join(new_operation_path, file_name))
                    datatype = self.load_datatype_from_file(new_operation_path, file_name,
                                                            operation_entity.id, datatype_group)
                    all_datatypes.append(datatype)

            # Before inserting into DB sort data types by creation date (to solve any dependencies)
            all_datatypes = sorted(all_datatypes, key=lambda datatype: datatype.create_date)

            # Now store data types into DB
            for datatype in all_datatypes:
                if datatype.gid in dt_burst_mappings:
                    old_burst_id = dt_burst_mappings[datatype.gid]
                    if old_burst_id is not None:
                        datatype.fk_parent_burst = burst_ids_mapping[old_burst_id]
                self.store_datatype(datatype)

            # Now import all images from current operation
            images_root = self.files_helper.get_images_folder(project.name, operation_entity.id)
            if os.path.exists(images_root):
                for root, _, files in os.walk(images_root):
                    for file_name in files:
                        if file_name.endswith(FilesHelper.TVB_FILE_EXTENSION):
                            self.__populate_image(os.path.join(root, file_name), project.id, operation_entity.id)
            imported_operations.append(operation_entity)

        return imported_operations


    def __populate_image(self, file_name, project_id, op_id):
        """
        Create and store a image entity.
        """
        figure_dict = XMLReader(file_name).read_metadata()
        new_path = os.path.join(os.path.split(file_name)[0],
                                os.path.split(figure_dict['file_path'])[1])
        if os.path.exists(new_path):
            figure_dict['fk_op_id'] = op_id
            figure_dict['fk_user_id'] = self.user_id
            figure_dict['fk_project_id'] = project_id
            figure_entity = manager_of_class(model.ResultFigure).new_instance()
            figure_entity = figure_entity.from_dict(figure_dict)
            stored_entity = dao.store_entity(figure_entity)

            # Update image meta-data with the new details after import 
            figure = dao.load_figure(stored_entity.id)
            self.logger.debug("Store imported figure")
            self.files_helper.write_image_metadata(figure)


    def load_datatype_from_file(self, storage_folder, file_name, op_id, datatype_group=None):
        """
        Creates an instance of datatype from storage / H5 file 
        :returns: datatype
        """
        self.logger.debug("Loading datatType from file: %s" % file_name)
        storage_manager = HDF5StorageManager(storage_folder, file_name)
        meta_dictionary = storage_manager.get_metadata()
        meta_structure = DataTypeMetaData(meta_dictionary)

        # Now try to determine class and instantiate it
        class_name = meta_structure[DataTypeMetaData.KEY_CLASS_NAME]
        class_module = meta_structure[DataTypeMetaData.KEY_MODULE]
        datatype = __import__(class_module, globals(), locals(), [class_name])
        datatype = eval("datatype." + class_name)
        type_instance = manager_of_class(datatype).new_instance()

        # Now we fill data into instance
        type_instance.type = str(type_instance.__class__.__name__)
        type_instance.module = str(type_instance.__module__)

        # Fill instance with meta data
        type_instance.load_from_metadata(meta_dictionary)

        #Add all the required attributes
        if datatype_group is not None:
            type_instance.fk_datatype_group = datatype_group.id
        type_instance.set_operation_id(op_id)

        # Now move storage file into correct folder if necessary
        current_file = os.path.join(storage_folder, file_name)
        new_file = type_instance.get_storage_file_path()
        if new_file != current_file:
            shutil.move(current_file, new_file)

        return type_instance


    def store_datatype(self, datatype):
        """This method stores data type into DB"""
        try:
            self.logger.debug("Store datatype: %s with Gid: %s" % (datatype.__class__.__name__, datatype.gid))
            return dao.store_entity(datatype)
        except MissingDataSetException:
            self.logger.error("Datatype %s has missing data and could not be imported properly." % (datatype,))
            os.remove(datatype.get_storage_file_path())
        except IntegrityError, excep:
            self.logger.exception(excep)
            error_msg = "Could not import data with gid: %s. There is already a one with " \
                        "the same name or gid." % datatype.gid
            # Delete file if can't be imported
            os.remove(datatype.get_storage_file_path())
            raise ProjectImportException(error_msg)


    def __populate_project(self, project_path):
        """
        Create and store a Project entity.
        """
        self.logger.debug("Creating project from path: %s" % project_path)
        project_cfg_file = os.path.join(project_path, FilesHelper.TVB_PROJECT_FILE)

        reader = XMLReader(project_cfg_file)
        project_dict = reader.read_metadata()
        project_entity = manager_of_class(model.Project).new_instance()
        project_entity = project_entity.from_dict(project_dict, self.user_id)

        try:
            self.logger.debug("Storing imported project")
            return dao.store_entity(project_entity)
        except IntegrityError, excep:
            self.logger.exception(excep)
            error_msg = ("Could not import project: %s with gid: %s. There is already a "
                         "project with the same name or gid.") % (project_entity.name, project_entity.gid)
            raise ProjectImportException(error_msg)


    def __build_operation_from_file(self, project, operation_file):
        """
        Create Operation entity from metadata file.
        """
        reader = XMLReader(operation_file)
        operation_dict = reader.read_metadata()
        operation_entity = manager_of_class(model.Operation).new_instance()

        return operation_entity.from_dict(operation_dict, dao, self.user_id, project.gid)


    @staticmethod
    def __import_operation(operation_entity):
        """
        Store a Operation entity.
        """
        operation_entity = dao.store_entity(operation_entity)
        operation_group_id = operation_entity.fk_operation_group
        datatype_group = None

        if operation_group_id is not None:
            try:
                datatype_group = dao.get_datatypegroup_by_op_group_id(operation_group_id)
            except Exception:
                # If no dataType group present for current op. group, create it.
                operation_group = dao.get_operationgroup_by_id(operation_group_id)
                datatype_group = model.DataTypeGroup(operation_group, operation_id=operation_entity.id)
                datatype_group.state = ADAPTERS['Upload']['defaultdatastate']
                datatype_group = dao.store_entity(datatype_group)

        return operation_entity, datatype_group
    
        