# coding: utf-8
"""
Firework workflows
"""
from __future__ import print_function, division, unicode_literals

import logging
import sys

import abc
import os
import six
import datetime
import numpy as np
from collections import defaultdict
from abipy.abio.factories import HybridOneShotFromGsFactory, ScfFactory, IoncellRelaxFromGsFactory
from abipy.abio.factories import PhononsFromGsFactory, ScfForPhononsFactory, InputFactory
from abipy.abio.factories import ion_ioncell_relax_input, scf_input
from abipy.abio.inputs import AbinitInput, AnaddbInput
from abipy.abio.abivars_db import get_abinit_variables
from abipy.abio.input_tags import *
from abipy.core.structure import Structure
from fireworks.core.firework import Firework, Workflow
from fireworks.core.launchpad import LaunchPad
from monty.serialization import loadfn

from abiflows.core.mastermind_abc import ControlProcedure
from abiflows.core.controllers import AbinitController, WalltimeController, MemoryController
from abiflows.fireworks.tasks.abinit_tasks import AbiFireTask, ScfFWTask, RelaxFWTask, NscfFWTask, PhononTask, BecTask
from abiflows.fireworks.tasks.abinit_tasks_src import AbinitSetupTask, AbinitRunTask, AbinitControlTask
from abiflows.fireworks.tasks.abinit_tasks_src import ScfTaskHelper, NscfTaskHelper, DdkTaskHelper
from abiflows.fireworks.tasks.abinit_tasks_src import RelaxTaskHelper
from abiflows.fireworks.tasks.abinit_tasks_src import GeneratePiezoElasticFlowFWSRCAbinitTask
from abiflows.fireworks.tasks.abinit_tasks_src import Cut3DAbinitTask
from abiflows.fireworks.tasks.abinit_tasks_src import BaderTask
from abiflows.fireworks.tasks.abinit_tasks import HybridFWTask, RelaxDilatmxFWTask, GeneratePhononFlowFWAbinitTask
from abiflows.fireworks.tasks.abinit_tasks import GeneratePiezoElasticFlowFWAbinitTask, AutoparalTask, DdeTask
from abiflows.fireworks.tasks.abinit_tasks import AnaDdbAbinitTask, StrainPertTask, DdkTask, MergeDdbAbinitTask
from abiflows.fireworks.tasks.handlers import MemoryHandler, WalltimeHandler
from abiflows.fireworks.tasks.src_tasks_abc import createSRCFireworks
from abiflows.fireworks.tasks.utility_tasks import FinalCleanUpTask, DatabaseInsertTask, MongoEngineDBInsertionTask
from abiflows.fireworks.tasks.utility_tasks import createSRCFireworksOld
from abiflows.fireworks.utils.fw_utils import append_fw_to_wf, get_short_single_core_spec, links_dict_update
from abiflows.fireworks.utils.fw_utils import set_short_single_core_to_spec, get_last_completed_launch
from abiflows.fireworks.utils.fw_utils import get_time_report_for_wf
from abiflows.database.mongoengine.abinit_results import RelaxResult, PhononResult
from abiflows.fireworks.utils.task_history import TaskEvent
from pymatgen.io.abinit.abiobjects import KSampling

# logging.basicConfig()
logger = logging.getLogger(__name__)
logger.addHandler(logging.StreamHandler(sys.stdout))



@six.add_metaclass(abc.ABCMeta)
class AbstractFWWorkflow(Workflow):
    """
    Abstract Workflow class.
    """

    def add_to_db(self, lpad=None):
        if not lpad:
            lpad = LaunchPad.auto_load()
        return lpad.add_wf(self.wf)

    def append_fw(self, fw, short_single_spec=False):
        if short_single_spec:
            fw.spec.update(self.set_short_single_core_to_spec())
        append_fw_to_wf(fw, self.wf)

    @staticmethod
    def set_short_single_core_to_spec(spec=None, master_mem_overhead=0):
        if spec is None:
                spec = {}
        spec = dict(spec)

        qadapter_spec = get_short_single_core_spec(master_mem_overhead=master_mem_overhead)
        spec['mpi_ncpus'] = 1
        spec['_queueadapter'] = qadapter_spec
        return spec

    def add_mongoengine_db_insertion(self, db_data):
        self.append_fw(Firework([MongoEngineDBInsertionTask(db_data=db_data)]), short_single_spec=True)

    def add_final_cleanup(self, out_exts=None, additional_spec=None):
        if out_exts is None:
            out_exts = ["WFK", "1WF", "DEN"]
        spec = self.set_short_single_core_to_spec()
        if additional_spec:
            spec.update(additional_spec)
        # high priority
        #TODO improve the handling of the priorities
        spec['_priority'] = 100
        cleanup_fw = Firework(FinalCleanUpTask(out_exts=out_exts), spec=spec,
                              name=(self.wf.name+"_cleanup")[:15])
        spec['_add_launchpad_and_fw_id'] = True

        append_fw_to_wf(cleanup_fw, self.wf)

    def add_db_insert_and_cleanup(self, mongo_database, out_exts=None, insertion_data=None,
                                  criteria=None):
        if out_exts is None:
            out_exts = ["WFK", "1WF", "DEN"]
        if insertion_data is None:
            insertion_data = {'structure': 'get_final_structure_and_history'}
        spec = self.set_short_single_core_to_spec()
        spec['mongo_database'] = mongo_database.as_dict()
        spec['_add_launchpad_and_fw_id'] = True
        insert_and_cleanup_fw = Firework([DatabaseInsertTask(insertion_data=insertion_data, criteria=criteria),
                                          FinalCleanUpTask(out_exts=out_exts)],
                                         spec=spec,
                                         name=(self.wf.name+"_insclnup")[:15])

        append_fw_to_wf(insert_and_cleanup_fw, self.wf)

    def add_cut3d_den_to_cube_task(self, den_task_type_source=None):
        spec = self.set_short_single_core_to_spec()
        spec['_add_launchpad_and_fw_id'] = True
        if den_task_type_source is None:
            cut3d_fw = Firework(Cut3DAbinitTask.den_to_cube(deps=['DEN']), spec=spec,
                                name=(self.wf.name+"_cut3d")[:15])
        else:
            raise NotImplementedError('Cut3D from specified task_type source not yet implemented')

        append_fw_to_wf(cut3d_fw, self.wf)

    def add_bader_task(self, den_task_type_source=None):
        spec = self.set_short_single_core_to_spec()
        spec['_add_launchpad_and_fw_id'] = True
        if den_task_type_source is None:
            den_task_type_source = 'scf'
        # Find the Firework that should compute the DEN file
        den_fw = None
        control_fw_id = None
        for fw_id, fw in self.wf.id_fw.items():
            for task in fw.tasks:
                if isinstance(task, AbinitSetupTask):
                    if task.task_type == den_task_type_source:
                        if den_fw is None:
                            den_fw = fw
                            if not task.pass_input:
                                raise ValueError('Abinit task with task_type "{}" should pass the input to the '
                                                 'Bader task'.format(den_task_type_source))
                            den_fw_id = fw_id
                            if len(self.wf.links[den_fw_id]) != 1:
                                raise ValueError('AbinitSetupTask has {:d} children while it should have exactly '
                                                 'one'.format(len(self.wf.links[den_fw_id])))
                            run_fw_id = self.wf.links[den_fw_id][0]
                            if len(self.wf.links[run_fw_id]) != 1:
                                raise ValueError('AbinitRunTask has {:d} children while it should have exactly '
                                                 'one'.format(len(self.wf.links[run_fw_id])))
                            control_fw_id = self.wf.links[run_fw_id][0]
                        else:
                            raise ValueError('Found more than one Firework with Abinit '
                                             'task_type "{}".'.format(den_task_type_source))
        if den_fw is None:
            raise ValueError('Firework with Abinit task_type "{}" not found.'.format(den_task_type_source))
        # # Set the pass_input variable of the task to True (needed to get the pseudo valence electrons)
        # for task in den_fw.tasks:
        #     if isinstance(task, AbinitSetupTask):
        #         task.pass_input = True
        spec['den_task_type_source'] = den_task_type_source
        cut3d_task = Cut3DAbinitTask.den_to_cube(deps=['DEN'])
        bader_task = BaderTask()
        bader_fw = Firework([cut3d_task, bader_task], spec=spec,
                            name=("bader")[:15])

        self.wf.append_wf(new_wf=Workflow.from_Firework(bader_fw), fw_ids=[control_fw_id],
                          detour=False, pull_spec_mods=False)

    @classmethod
    def get_bader_charges(cls, wf):
        # I dont think we need that here ...
        # assert wf.metadata['workflow_class'] == self.workflow_class
        # assert wf.metadata['workflow_module'] == self.workflow_module
        final_fw_id = None
        for fw_id, fw in wf.id_fw.items():
            if fw.name == 'bader':
                if not final_fw_id:
                    final_fw_id = fw_id
                else:
                    raise ValueError('Multiple Fireworks found with name equal to "bader"')
        if final_fw_id is None:
            raise RuntimeError('Bader analysis not found ...')
        myfw = wf.id_fw[final_fw_id]
        #TODO add a check on the state of the launches
        last_launch = (myfw.archived_launches + myfw.launches)[-1]
        #TODO add a cycle to find the instance of AbiFireTask?
        myfw.tasks[-1].setup_rundir(rundir=last_launch.launch_dir)
        bader_data = myfw.tasks[-1].get_bader_data()
        if len(myfw.spec['previous_fws'][myfw.spec['den_task_type_source']]) != 1:
            raise ValueError('Found "{:d}" previous fws with task_type "{}" while there should be only '
                             'one.'.format(len(myfw.spec['previous_fws'][myfw.spec['den_task_type_source']]),
                                           myfw.spec['den_task_type_source']))
        abinit_input = myfw.spec['previous_fws'][myfw.spec['den_task_type_source']][0]['input']
        psp_valences = abinit_input.valence_electrons_per_atom
        bader_charges = [atom['charge'] for atom in bader_data]
        bader_charges_transfer = [bader_charges[iatom]-psp_valences[iatom] for iatom in range(len(psp_valences))]

        return {'bader_analysis': {'pseudo_valence_charges': psp_valences,
                                   'bader_charges': bader_charges,
                                   'bader_charges_transfer': bader_charges_transfer}}

    def add_metadata(self, structure=None, additional_metadata=None):
        if additional_metadata is None:
            additional_metadata = {}
        metadata = dict(wf_type = self.__class__.__name__)
        if structure:
            composition = structure.composition
            metadata['nsites'] = len(structure)
            metadata['elements'] = [el.symbol for el in composition.elements]
            metadata['reduced_formula'] = composition.reduced_formula

        metadata.update(additional_metadata)

        self.wf.metadata.update(metadata)

    def get_reduced_formula(self, input):
        structure = None
        try:
            if isinstance(input, AbinitInput):
                structure = input.structure
            elif 'structure' in input.kwargs:
                structure = input.kwargs['structure']
            else:
                structure = input.args[0]
        except Exception as e:
            logger.warning("Couldn't get the structure from the input: {} {}".format(e.__class__.__name__, e.message))

        return structure.composition.reduced_formula if structure else ""

    def add_spec_to_all_fws(self, spec):
        for fw in self.wf.fws:
            fw.spec.update(spec)

    def set_preserve_fworker(self):
        self.add_spec_to_all_fws(dict(_preserve_fworker=True))

    def fix_fworker(self, name=None):
        """
        Sets the _fworker key to the name specified and adds _preserve_fworker to the spec of all the fws.
        If name is None the name is taken from ~/.fireworks/my_fworker.yaml
        """
        if name == None:
            name = loadfn(os.path.expanduser("~/.fireworks/my_fworker.yaml"))['name']

        self.add_spec_to_all_fws(dict(_preserve_fworker=True, _fworker=name))


class InputFWWorkflow(AbstractFWWorkflow):
    def __init__(self, abiinput, task_type=AbiFireTask, autoparal=False, spec=None, initialization_info=None):
        if spec is None:
            spec = {}
        if initialization_info is None:
            initialization_info = {}
        abitask = task_type(abiinput, is_autoparal=autoparal)

        spec = dict(spec)
        spec['initialization_info'] = initialization_info
        if autoparal:
            spec = self.set_short_single_core_to_spec(spec)

        self.fw = Firework(abitask, spec=spec)

        self.wf = Workflow([self.fw])
        # Workflow.__init__([self.fw])


class ScfFWWorkflow(AbstractFWWorkflow):
    def __init__(self, abiinput, autoparal=False, spec=None, initialization_info=None):
        if spec is None:
            spec = {}
        if initialization_info is None:
            initialization_info = {}
        abitask = ScfFWTask(abiinput, is_autoparal=autoparal)

        spec = dict(spec)
        spec['initialization_info'] = initialization_info
        if autoparal:
            spec = self.set_short_single_core_to_spec(spec)

        self.scf_fw = Firework(abitask, spec=spec)

        self.wf = Workflow([self.scf_fw])

    @classmethod
    def from_factory(cls, structure, pseudos, kppa=None, ecut=None, pawecutdg=None, nband=None, accuracy="normal",
                     spin_mode="polarized", smearing="fermi_dirac:0.1 eV", charge=0.0, scf_algorithm=None,
                     shift_mode="Monkhorst-Pack", extra_abivars=None, decorators=None, autoparal=False, spec=None,
                     initialization_info=None):

        if extra_abivars is None:
                extra_abivars = {}
        if decorators is None:
                decorators = []
        if spec is None:
                spec = {}
        if initialization_info is None:
            initialization_info = {}

        abiinput = scf_input(structure, pseudos, kppa=kppa, ecut=ecut, pawecutdg=pawecutdg, nband=nband,
                             accuracy=accuracy, spin_mode=spin_mode, smearing=smearing, charge=charge,
                             scf_algorithm=scf_algorithm, shift_mode=shift_mode)
        abiinput.set_vars(extra_abivars)
        for d in decorators:
            d(abiinput)

        return cls(abiinput, autoparal=autoparal, spec=spec, initialization_info=initialization_info)


class ScfFWWorkflowSRC(AbstractFWWorkflow):

    workflow_class = 'ScfFWWorkflowSRC'
    workflow_module = 'abiflows.fireworks.workflows.abinit_workflows'

    def __init__(self, abiinput, spec=None, initialization_info=None, pass_input=False):
        if spec is None:
            spec = {}
        if initialization_info is None:
            initialization_info = {}

        scf_helper = ScfTaskHelper()
        control_procedure = ControlProcedure(controllers=[AbinitController.from_helper(scf_helper),
                                                          WalltimeController(), MemoryController()])
        setup_task = AbinitSetupTask(abiinput=abiinput, task_helper=scf_helper, pass_input=pass_input)
        run_task = AbinitRunTask(control_procedure=control_procedure, task_helper=scf_helper)
        control_task = AbinitControlTask(control_procedure=control_procedure, task_helper=scf_helper)

        scf_fws = createSRCFireworks(setup_task=setup_task, run_task=run_task, control_task=control_task, spec=spec,
                                     task_index='scf', initialization_info=initialization_info)

        self.wf = Workflow(fireworks=scf_fws['fws'], links_dict=scf_fws['links_dict'],
                           metadata={'workflow_class': self.workflow_class,
                                     'workflow_module': self.workflow_module})

    @classmethod
    def from_factory(cls, structure, pseudos, kppa=None, ecut=None, pawecutdg=None, nband=None, accuracy="normal",
                     spin_mode="polarized", smearing="fermi_dirac:0.1 eV", charge=0.0, scf_algorithm=None,
                     shift_mode="Monkhorst-Pack", extra_abivars=None, decorators=None, autoparal=False, spec=None,
                     initialization_info=None, pass_input=False):
        if extra_abivars is None:
                extra_abivars = {}
        if decorators is None:
                decorators = []
        if spec is None:
                spec = {}
        if initialization_info is None:
            initialization_info = {}

        abiinput = scf_input(structure, pseudos, kppa=kppa, ecut=ecut, pawecutdg=pawecutdg, nband=nband,
                             accuracy=accuracy, spin_mode=spin_mode, smearing=smearing, charge=charge,
                             scf_algorithm=scf_algorithm, shift_mode=shift_mode)
        abiinput.set_vars(extra_abivars)
        for d in decorators:
            d(abiinput)

        return cls(abiinput, spec=spec, initialization_info=initialization_info, pass_input=pass_input)


class RelaxFWWorkflow(AbstractFWWorkflow):
    workflow_class = 'RelaxFWWorkflow'
    workflow_module = 'abiflows.fireworks.workflows.abinit_workflows'

    def __init__(self, ion_input, ioncell_input, autoparal=False, spec=None, initialization_info=None, target_dilatmx=None,
                 skip_ion=False):
        if spec is None:
            spec = {}
        if initialization_info is None:
            initialization_info = {}

        start_task_index = 1
        spec = dict(spec)
        spec['initialization_info'] = initialization_info
        if autoparal:
            spec = self.set_short_single_core_to_spec(spec)
            start_task_index = 'autoparal'

        fws = []
        deps = {}

        if not skip_ion:
            spec['wf_task_index'] = 'ion_' + str(start_task_index)
            ion_task = RelaxFWTask(ion_input, is_autoparal=autoparal)
            self.ion_fw = Firework(ion_task, spec=spec)
            deps = {ion_task.task_type: '@structure'}
            fws.append(self.ion_fw)

        spec['wf_task_index'] = 'ioncell_' + str(start_task_index)
        if target_dilatmx:
            ioncell_task = RelaxDilatmxFWTask(ioncell_input, is_autoparal=autoparal, target_dilatmx=target_dilatmx,
                                              deps=deps)
        else:
            ioncell_task = RelaxFWTask(ioncell_input, is_autoparal=autoparal, deps=deps)

        self.ioncell_fw = Firework(ioncell_task, spec=spec)

        fws.append(self.ioncell_fw)

        fw_deps = None if skip_ion else {self.ion_fw: [self.ioncell_fw]}

        self.wf = Workflow(fws, fw_deps,
                           metadata={'workflow_class': self.workflow_class,
                                     'workflow_module': self.workflow_module})

    @classmethod
    def get_final_structure_and_history(cls, wf):
        assert wf.metadata['workflow_class'] == cls.workflow_class
        assert wf.metadata['workflow_module'] == cls.workflow_module
        ioncell = -1
        final_fw_id = None
        for fw_id, fw in wf.id_fw.items():
            if 'wf_task_index' in fw.spec and fw.spec['wf_task_index'][:8] == 'ioncell_':
                try:
                    this_ioncell =  int(fw.spec['wf_task_index'].split('_')[-1])
                except ValueError:
                    # skip if the index is not an int
                    continue
                if this_ioncell > ioncell:
                    ioncell = this_ioncell
                    final_fw_id = fw_id
        if final_fw_id is None:
            raise RuntimeError('Final structure not found ...')
        myfw = wf.id_fw[final_fw_id]
        #TODO add a check on the state of the launches
        last_launch = (myfw.archived_launches + myfw.launches)[-1]
        #TODO add a cycle to find the instance of AbiFireTask?
        myfw.tasks[-1].set_workdir(workdir=last_launch.launch_dir)
        structure = myfw.tasks[-1].get_final_structure()
        history = loadfn(os.path.join(last_launch.launch_dir, 'history.json'))

        return {'structure': structure.as_dict(), 'history': history}

    @classmethod
    def get_runtime_secs(cls, wf):
        assert wf.metadata['workflow_class'] == cls.workflow_class
        assert wf.metadata['workflow_module'] == cls.workflow_module
        time_secs = 0.0
        for fw_id, fw in wf.id_fw.items():
            if 'wf_task_index' in fw.spec:
                if fw.spec['wf_task_index'][-9:] == 'autoparal':
                    time_secs += fw.launches[-1].runtime_secs
                elif fw.spec['wf_task_index'][:4] == 'ion_':
                    time_secs += fw.launches[-1].runtime_secs * fw.spec['mpi_ncpus']
                elif fw.spec['wf_task_index'][:8] == 'ioncell_':
                    time_secs += fw.launches[-1].runtime_secs * fw.spec['mpi_ncpus']
        return time_secs

    @classmethod
    def get_mongoengine_results(cls, wf):
        assert wf.metadata['workflow_class'] == cls.workflow_class
        assert wf.metadata['workflow_module'] == cls.workflow_module

        ioncell_fws = [fw for fw in wf.fws if fw.spec.get('wf_task_index', '').startswith('ioncell_') and not fw.spec.get('wf_task_index', '').endswith('autoparal')]
        ioncell_fws.sort(key=lambda l: int(l.spec.get('wf_task_index', '0').split('_')[-1]))
        last_ioncell_fw = ioncell_fws[-1]
        last_ioncell_launch = get_last_completed_launch(last_ioncell_fw)

        ion_fws = [fw for fw in wf.fws if fw.spec.get('wf_task_index', '').startswith('ion_') and not fw.spec.get('wf_task_index', '').endswith('autoparal')]
        ion_fws.sort(key=lambda l: int(l.spec.get('wf_task_index', '0').split('_')[-1]))
        first_ion_fw = ioncell_fws[0]
        last_ion_fw = ioncell_fws[-1]
        last_ion_launch = get_last_completed_launch(last_ion_fw)

        relax_task = last_ioncell_fw.tasks[-1]
        relax_task.set_workdir(workdir=last_ioncell_launch.launch_dir)
        structure = relax_task.get_final_structure()
        history_ioncell = loadfn(os.path.join(last_ioncell_launch.launch_dir, 'history.json'))
        history_ion = loadfn(os.path.join(last_ion_launch.launch_dir, 'history.json'))

        document = RelaxResult()

        document.abinit_output.structure = structure.as_dict()
        document.set_material_data_from_structure(structure)

        final_input = history_ioncell.get_events_by_types(TaskEvent.FINALIZED)[0].details['final_input']
        document.abinit_input.last_input = final_input.as_dict()
        document.abinit_input.set_abinit_basic_from_abinit_input(final_input)
        # need to set the structure as the initial one
        document.abinit_input.structure = first_ion_fw.tasks[0].abiinput.structure.as_dict()

        document.history = history_ioncell.as_dict()

        document.set_dir_names_from_fws_wf(wf)

        initialization_info = history_ioncell.get_events_by_types(TaskEvent.INITIALIZED)[0].details.get('initialization_info', {})
        document.abinit_input.kppa = initialization_info.get('kppa', None)
        document.mp_id = initialization_info.get('mp_id', None)

        document.abinit_input.pseudopotentials.set_pseudos_from_files_file(relax_task.files_file.path, len(structure.composition.elements))

        document.time_report = get_time_report_for_wf(wf).as_dict()

        document.fw_id = last_ion_fw.fw_id

        document.created_on = datetime.datetime.now()
        document.modified_on = datetime.datetime.now()

        with open(relax_task.gsr_path, "rb") as f:
            document.abinit_output.gsr.put(f)

        # first get all the file paths. If something goes wrong in this loop no file is left dangling in the db
        hist_files_path = {}
        for fw in ion_fws + ioncell_fws:
            task_index = fw.spec.get('wf_task_index')
            last_launch = get_last_completed_launch(fw)
            task = fw.tasks[0]
            task.set_workdir(workdir=last_launch.launch_dir)
            hist_files_path[task_index] = task.hist_nc_path

        # now save all the files in the db
        #TODO I would prefer to avoid the import of mongoengine related objects here and delegate to some other specific module
        #from abiflows.core.models import AbiGridFSProxy
        # This is an alternative from importing the object explicitely. Still quite a dirty hack
        proxy_class = RelaxResult.abinit_output.default.hist_files.field.proxy_class
        collection_name = RelaxResult.abinit_output.default.hist_files.field.collection_name
        hist_files = {}
        for task_index, file_path in six.iteritems(hist_files_path):
            with open(file_path) as f:
                file_field = proxy_class(collection_name=collection_name)
                file_field.put(f)
                hist_files[task_index] = file_field

        with open(relax_task.output_file.path, 'rt') as f:
            document.abinit_output.outfile_ioncell.put(f)

        document.abinit_output.hist_files = hist_files

        return document

    @classmethod
    def from_factory(cls, structure, pseudos, kppa=None, nband=None, ecut=None, pawecutdg=None, accuracy="normal",
                     spin_mode="polarized", smearing="fermi_dirac:0.1 eV", charge=0.0, scf_algorithm=None,
                     extra_abivars=None, decorators=None, autoparal=False, spec=None, initialization_info=None,
                     target_dilatmx=None, skip_ion=False, shift_mode="Monkhorst-Pack"):

        if extra_abivars is None:
                extra_abivars = {}
        if decorators is None:
                decorators = []
        if spec is None:
                spec = {}
        if initialization_info is None:
                initialization_info = {}
        ion_input = ion_ioncell_relax_input(structure=structure, pseudos=pseudos, kppa=kppa, nband=nband, ecut=ecut,
                                            pawecutdg=pawecutdg, accuracy=accuracy, spin_mode=spin_mode,
                                            smearing=smearing, charge=charge, scf_algorithm=scf_algorithm,
                                            shift_mode=shift_mode)[0]

        ion_input.set_vars(**extra_abivars)
        for d in decorators:
            ion_input = d(ion_input)

        ioncell_fact = IoncellRelaxFromGsFactory(accuracy=accuracy, extra_abivars=extra_abivars, decorators=decorators)

        return cls(ion_input, ioncell_fact, autoparal=autoparal, spec=spec, initialization_info=initialization_info,
                   target_dilatmx=target_dilatmx,skip_ion=skip_ion)


class RelaxFWWorkflowSRCOld(AbstractFWWorkflow):
    workflow_class = 'RelaxFWWorkflowSRC'
    workflow_module = 'abiflows.fireworks.workflows.abinit_workflows'

    def __init__(self, ion_input, ioncell_input, spec=None, initialization_info=None):
        if spec is None:
                spec = {}
        if initialization_info is None:
                initialization_info = {}

        fws = []
        links_dict = {}

        if 'queue_adapter_update' in initialization_info:
            queue_adapter_update = initialization_info['queue_adapter_update']
        else:
            queue_adapter_update = None

        SRC_ion_fws = createSRCFireworksOld(task_class=RelaxFWTask, task_input=ion_input, SRC_spec=spec,
                                            initialization_info=initialization_info,
                                            wf_task_index_prefix='ion', queue_adapter_update=queue_adapter_update)
        fws.extend(SRC_ion_fws['fws'])
        links_dict.update(SRC_ion_fws['links_dict'])

        SRC_ioncell_fws = createSRCFireworksOld(task_class=RelaxFWTask, task_input=ioncell_input, SRC_spec=spec,
                                                initialization_info=initialization_info,
                                                wf_task_index_prefix='ioncell',
                                                deps={SRC_ion_fws['run_fw'].tasks[0].task_type: '@structure'},
                                                queue_adapter_update=queue_adapter_update)
        fws.extend(SRC_ioncell_fws['fws'])
        links_dict.update(SRC_ioncell_fws['links_dict'])

        links_dict.update({SRC_ion_fws['check_fw']: SRC_ioncell_fws['setup_fw']})

        self.wf = Workflow(fireworks=fws,
                           links_dict=links_dict,
                           metadata={'workflow_class': self.workflow_class,
                                     'workflow_module': self.workflow_module})

    @classmethod
    def get_final_structure_and_history(cls, wf):
        assert wf.metadata['workflow_class'] == cls.workflow_class
        assert wf.metadata['workflow_module'] == cls.workflow_module
        ioncell = -1
        final_fw_id = None
        for fw_id, fw in wf.id_fw.items():
            if 'wf_task_index' in fw.spec:
                if fw.spec['wf_task_index'][:12] == 'run_ioncell_':
                    try:
                        this_ioncell =  int(fw.spec['wf_task_index'].split('_')[-1])
                    except ValueError:
                        # skip if the index is not an int
                        continue
                    if this_ioncell > ioncell:
                        ioncell = this_ioncell
                        final_fw_id = fw_id
        if final_fw_id is None:
            raise RuntimeError('Final structure not found ...')
        myfw = wf.id_fw[final_fw_id]
        #TODO add a check on the state of the launches
        last_launch = (myfw.archived_launches + myfw.launches)[-1]
        #TODO add a cycle to find the instance of AbiFireTask?
        myfw.tasks[-1].set_workdir(workdir=last_launch.launch_dir)
        structure = myfw.tasks[-1].get_final_structure()
        history = loadfn(os.path.join(last_launch.launch_dir, 'history.json'))

        return {'structure': structure.as_dict(), 'history': history}


class RelaxFWWorkflowSRC(AbstractFWWorkflow):
    workflow_class = 'RelaxFWWorkflowSRC'
    workflow_module = 'abiflows.fireworks.workflows.abinit_workflows'

    def __init__(self, ion_input, ioncell_input, spec=None, initialization_info=None, additional_controllers=None):
        if spec is None:
                spec = {}
        if initialization_info is None:
                initialization_info = {}

        fws = []
        links_dict = {}

        if additional_controllers is None:
            additional_controllers = [WalltimeController(), MemoryController()]
        else:
            additional_controllers = additional_controllers

        #1. Relax run at fixed cell
        relax_helper = RelaxTaskHelper()
        relax_controllers = [AbinitController.from_helper(relax_helper)]
        relax_controllers.extend(additional_controllers)
        relax_control_procedure = ControlProcedure(controllers=relax_controllers)
        setup_relax_ions_task = AbinitSetupTask(abiinput=ion_input, task_helper=relax_helper)
        run_relax_ions_task = AbinitRunTask(control_procedure=relax_control_procedure, task_helper=relax_helper,
                                            task_type='ion')
        control_relax_ions_task = AbinitControlTask(control_procedure=relax_control_procedure,
                                                    task_helper=relax_helper)

        relax_ions_fws = createSRCFireworks(setup_task=setup_relax_ions_task, run_task=run_relax_ions_task,
                                            control_task=control_relax_ions_task,
                                            spec=spec, initialization_info=initialization_info)

        fws.extend(relax_ions_fws['fws'])
        links_dict_update(links_dict=links_dict, links_update=relax_ions_fws['links_dict'])

        #2. Relax run with cell relaxation
        setup_relax_ions_cell_task = AbinitSetupTask(abiinput=ioncell_input, task_helper=relax_helper,
                                                     deps={run_relax_ions_task.task_type: '@structure'})
        run_relax_ions_cell_task = AbinitRunTask(control_procedure=relax_control_procedure, task_helper=relax_helper,
                                                 task_type='ioncell')
        control_relax_ions_cell_task = AbinitControlTask(control_procedure=relax_control_procedure,
                                                         task_helper=relax_helper)

        relax_ions_cell_fws = createSRCFireworks(setup_task=setup_relax_ions_cell_task,
                                                 run_task=run_relax_ions_cell_task,
                                                 control_task=control_relax_ions_cell_task,
                                                 spec=spec, initialization_info=initialization_info)

        fws.extend(relax_ions_cell_fws['fws'])
        links_dict_update(links_dict=links_dict, links_update=relax_ions_cell_fws['links_dict'])

        links_dict.update({relax_ions_fws['control_fw']: relax_ions_cell_fws['setup_fw']})

        self.wf = Workflow(fireworks=fws,
                           links_dict=links_dict,
                           metadata={'workflow_class': self.workflow_class,
                                     'workflow_module': self.workflow_module})

    @classmethod
    def get_final_structure(cls, wf):
        assert wf.metadata['workflow_class'] == cls.workflow_class
        assert wf.metadata['workflow_module'] == cls.workflow_module
        ioncell = -1
        final_fw_id = None
        for fw_id, fw in wf.id_fw.items():
            if 'SRC_task_index' in fw.spec:
                if fw.tasks[-1].src_type != 'run':
                    continue
                task_index = fw.spec['SRC_task_index']
                if task_index.task_type == 'ioncell':
                    if task_index.index > ioncell:
                        ioncell = task_index.index
                        final_fw_id = fw_id
        if final_fw_id is None:
            raise RuntimeError('Final structure not found ...')
        myfw = wf.id_fw[final_fw_id]
        mytask = myfw.tasks[-1]
        #TODO add a check on the state of the launches
        last_launch = (myfw.archived_launches + myfw.launches)[-1]
        #TODO add a cycle to find the instance of AbiFireTask?
        # myfw.tasks[-1].set_workdir(workdir=last_launch.launch_dir)
        # mytask.setup_rundir(last_launch.launch_dir, create_dirs=False)
        helper = RelaxTaskHelper()
        helper.set_task(mytask)
        helper.task.setup_rundir(last_launch.launch_dir, create_dirs=False)

        structure = helper.get_final_structure()

        return {'structure': structure.as_dict()}

    @classmethod
    def get_final_structure_and_history(cls, wf):
        assert wf.metadata['workflow_class'] == cls.workflow_class
        assert wf.metadata['workflow_module'] == cls.workflow_module
        ioncell = -1
        final_fw_id = None
        for fw_id, fw in wf.id_fw.items():
            if 'SRC_task_index' in fw.spec:
                if fw.tasks[-1].src_type != 'run':
                    continue
                task_index = fw.spec['SRC_task_index']
                if task_index.task_type == 'ioncell':
                    if task_index.index > ioncell:
                        ioncell = task_index.index
                        final_fw_id = fw_id
        if final_fw_id is None:
            raise RuntimeError('Final structure not found ...')
        myfw = wf.id_fw[final_fw_id]
        mytask = myfw.tasks[-1]
        #TODO add a check on the state of the launches
        last_launch = (myfw.archived_launches + myfw.launches)[-1]
        #TODO add a cycle to find the instance of AbiFireTask?
        # myfw.tasks[-1].set_workdir(workdir=last_launch.launch_dir)
        # mytask.setup_rundir(last_launch.launch_dir, create_dirs=False)
        helper = RelaxTaskHelper()
        helper.set_task(mytask)
        helper.task.setup_rundir(last_launch.launch_dir, create_dirs=False)
        # helper.set_task(mytask)

        structure = helper.get_final_structure()
        # history = loadfn(os.path.join(last_launch.launch_dir, 'history.json'))

        return {'structure': structure.as_dict()}

    @classmethod
    def get_computed_entry(cls, wf):
        assert wf.metadata['workflow_class'] == cls.workflow_class
        assert wf.metadata['workflow_module'] == cls.workflow_module
        ioncell = -1
        final_fw_id = None
        for fw_id, fw in wf.id_fw.items():
            if 'SRC_task_index' in fw.spec:
                if fw.tasks[-1].src_type != 'run':
                    continue
                task_index = fw.spec['SRC_task_index']
                if task_index.task_type == 'ioncell':
                    if task_index.index > ioncell:
                        ioncell = task_index.index
                        final_fw_id = fw_id
        if final_fw_id is None:
            raise RuntimeError('Final structure not found ...')
        myfw = wf.id_fw[final_fw_id]
        mytask = myfw.tasks[-1]
        #TODO add a check on the state of the launches
        last_launch = (myfw.archived_launches + myfw.launches)[-1]
        #TODO add a cycle to find the instance of AbiFireTask?
        # myfw.tasks[-1].set_workdir(workdir=last_launch.launch_dir)
        # mytask.setup_rundir(last_launch.launch_dir, create_dirs=False)
        helper = RelaxTaskHelper()
        helper.set_task(mytask)
        helper.task.setup_rundir(last_launch.launch_dir, create_dirs=False)
        # helper.set_task(mytask)

        computed_entry = helper.get_computed_entry()
        # history = loadfn(os.path.join(last_launch.launch_dir, 'history.json'))

        return {'computed_entry': computed_entry.as_dict()}


class NscfFWWorkflow(AbstractFWWorkflow):
    def __init__(self, scf_input, nscf_input, autoparal=False, spec=None, initialization_info=None):

        start_task_index = 1
        if spec is None:
            spec = {}
        if initialization_info is None:
            initialization_info = {}

        spec = dict(spec)
        spec['initialization_info'] = initialization_info
        if autoparal:
            spec = self.set_short_single_core_to_spec(spec)
            start_task_index = "autoparal"

        spec['wf_task_index'] = 'scf_' + str(start_task_index)
        scf_task = ScfFWTask(scf_input, is_autoparal=autoparal)
        self.scf_fw = Firework(scf_task, spec=spec)

        spec['wf_task_index'] = 'nscf_' + str(start_task_index)
        nscf_task = NscfFWTask(nscf_input, deps={scf_task.task_type: 'DEN'}, is_autoparal=autoparal)
        self.nscf_fw = Firework(nscf_task, spec=spec)

        self.wf = Workflow([self.scf_fw, self.nscf_fw], {self.scf_fw: [self.nscf_fw]},
                           metadata={'workflow_class': self.workflow_class,
                                     'workflow_module': self.workflow_module})


class NscfFWWorkflowSRC(AbstractFWWorkflow):
    workflow_class = 'NscfFWWorkflowSRC'
    workflow_module = 'abiflows.fireworks.workflows.abinit_workflows'

    def __init__(self, scf_input, nscf_input, spec=None, initialization_info=None):
        if spec is None:
            spec = {}
        if initialization_info is None:
            initialization_info = {}

        # Initializes fws list and links_dict
        fws = []
        links_dict = {}

        if 'additional_controllers' in spec:
            additional_controllers = spec['additional_controllers']
            spec.pop('additional_controllers')
        else:
            additional_controllers = [WalltimeController(), MemoryController()]
        # Self-consistent calculation
        scf_helper = ScfTaskHelper()
        scf_controllers = [AbinitController.from_helper(scf_helper)]
        scf_controllers.extend(additional_controllers)
        scf_control_procedure = ControlProcedure(controllers=scf_controllers)
        setup_scf_task = AbinitSetupTask(abiinput=scf_input, task_helper=scf_helper)
        run_scf_task = AbinitRunTask(control_procedure=scf_control_procedure, task_helper=scf_helper)
        control_scf_task = AbinitControlTask(control_procedure=scf_control_procedure, task_helper=scf_helper)

        scf_fws = createSRCFireworks(setup_task=setup_scf_task, run_task=run_scf_task, control_task=control_scf_task,
                                     task_index=scf_helper.task_type,
                                     spec=spec, initialization_info=initialization_info)

        fws.extend(scf_fws['fws'])
        links_dict_update(links_dict=links_dict, links_update=scf_fws['links_dict'])


        # Non self-consistent calculation
        nscf_helper = NscfTaskHelper()
        nscf_controllers = [AbinitController.from_helper(nscf_helper)]
        nscf_controllers.extend(additional_controllers)
        nscf_control_procedure = ControlProcedure(controllers=nscf_controllers)
        setup_nscf_task = AbinitSetupTask(abiinput=nscf_input, task_helper=nscf_helper,
                                          deps={run_scf_task.task_type: 'DEN'})
        run_nscf_task = AbinitRunTask(control_procedure=nscf_control_procedure, task_helper=nscf_helper)
        control_nscf_task = AbinitControlTask(control_procedure=nscf_control_procedure, task_helper=nscf_helper)

        nscf_fws = createSRCFireworks(setup_task=setup_nscf_task, run_task=run_nscf_task,
                                      control_task=control_nscf_task, task_index=nscf_helper.task_type, spec=spec,
                                      initialization_info=initialization_info)

        fws.extend(nscf_fws['fws'])
        links_dict_update(links_dict=links_dict, links_update=nscf_fws['links_dict'])
        #Link with previous SCF
        links_dict_update(links_dict=links_dict,
                          links_update={scf_fws['control_fw'].fw_id: nscf_fws['setup_fw'].fw_id})

        self.wf = Workflow(fireworks=fws, links_dict=links_dict,
                           metadata={'workflow_class': self.workflow_class,
                                     'workflow_module': self.workflow_module})

    @classmethod
    def from_factory(cls, structure, pseudos, kppa=None, ecut=None, pawecutdg=None, nband=None, accuracy="normal",
                     spin_mode="polarized", smearing="fermi_dirac:0.1 eV", charge=0.0, scf_algorithm=None,
                     shift_mode="Monkhorst-Pack", extra_abivars=None, decorators=None, autoparal=False, spec=None,
                     initialization_info=None):
        if extra_abivars is None:
            extra_abivars = {}
        if decorators is None:
            decorators = []
        if spec is None:
            spec = {}
        if initialization_info is None:
            initialization_info = {}
        raise NotImplementedError('from_factory class method not yet implemented for NscfWorkflowSRC')


class HybridOneShotFWWorkflow(AbstractFWWorkflow):
    def __init__(self, scf_inp, hybrid_input, autoparal=False, spec=None, initialization_info=None):
        if spec is None:
            spec = {}
        if initialization_info is None:
            initialization_info = {}
        rf = self.get_reduced_formula(scf_inp)

        scf_task = ScfFWTask(scf_inp, is_autoparal=autoparal)

        spec = dict(spec)
        spec['initialization_info'] = initialization_info
        if autoparal:
            spec = self.set_short_single_core_to_spec(spec)

        self.scf_fw = Firework(scf_task, spec=spec, name=rf+"_"+scf_task.task_type)

        hybrid_task = HybridFWTask(hybrid_input, is_autoparal=autoparal, deps=["WFK"])

        self.hybrid_fw = Firework(hybrid_task, spec=spec, name=rf+"_"+hybrid_task.task_type)

        self.wf = Workflow([self.scf_fw, self.hybrid_fw], {self.scf_fw: self.hybrid_fw})

    @classmethod
    def from_factory(cls, structure, pseudos, kppa=None, ecut=None, pawecutdg=None, nband=None, accuracy="normal",
                     spin_mode="polarized", smearing="fermi_dirac:0.1 eV", charge=0.0, scf_algorithm=None,
                     shift_mode="Monkhorst-Pack", hybrid_functional="hse06", ecutsigx=None, gw_qprange=1,
                     extra_abivars=None, decorators=None, autoparal=False, spec=None, initialization_info=None):

        if extra_abivars is None:
            extra_abivars = {}
        if decorators is None:
            decorators = []
        if spec is None:
            spec = {}
        if initialization_info is None:
                initialization_info = {}
        scf_fact = ScfFactory(structure=structure, pseudos=pseudos, kppa=kppa, ecut=ecut, pawecutdg=pawecutdg,
                              nband=nband, accuracy=accuracy, spin_mode=spin_mode, smearing=smearing, charge=charge,
                              scf_algorithm=scf_algorithm, shift_mode=shift_mode, extra_abivars=extra_abivars,
                              decorators=decorators)

        hybrid_fact = HybridOneShotFromGsFactory(functional=hybrid_functional, ecutsigx=ecutsigx, gw_qprange=gw_qprange,
                                                 decorators=decorators, extra_abivars=extra_abivars)

        return cls(scf_fact, hybrid_fact, autoparal=autoparal, spec=spec, initialization_info=initialization_info)


# class NscfFWWorkflow(AbstractFWWorkflow):
#     def __init__(self, scf_input, nscf_input, autoparal=False, spec={}):
#
#         spec = dict(spec)
#         if autoparal:
#             spec = self.set_short_single_core_to_spec(spec)
#
#         ion_task = ScfFWTask(scf_input, is_autoparal=autoparal)
#         self.ion_fw = Firework(ion_task, spec=spec)
#
#         ioncell_task = NscfFWTask(nscf_input, deps={ion_task.task_type: 'DEN'}, is_autoparal=autoparal)
#         self.ioncell_fw = Firework(ioncell_task, spec=spec)
#
#         self.wf = Workflow([self.ion_fw, self.ioncell_fw], {self.ion_fw: [self.ioncell_fw]})


class PhononFWWorkflowOld(AbstractFWWorkflow):
    workflow_class = 'PhononFWWorkflowOld'
    workflow_module = 'abiflows.fireworks.workflows.abinit_workflows'

    def __init__(self, scf_inp, phonon_factory, autoparal=False, spec=None, initialization_info=None):
        start_task_index = 1

        if spec is None:
            spec = {}
        if initialization_info is None:
            initialization_info = {}

        rf = self.get_reduced_formula(scf_inp)

        scf_task = ScfFWTask(scf_inp, is_autoparal=autoparal)

        spec = dict(spec)
        spec['initialization_info'] = initialization_info
        if autoparal:
            spec = self.set_short_single_core_to_spec(spec)
            start_task_index = 'autoparal'

        spec['wf_task_index'] = 'scf_' + str(start_task_index)


        self.scf_fw = Firework(scf_task, spec=spec, name=rf+"_"+scf_task.task_type)

        ph_generation_task = GeneratePhononFlowFWAbinitTask(phonon_factory, previous_task_type=scf_task.task_type,
                                                            with_autoparal=autoparal)

        spec['wf_task_index'] = 'gen_ph'

        self.ph_generation_fw = Firework(ph_generation_task, spec=spec, name=rf+"_gen_ph")

        self.wf = Workflow([self.scf_fw, self.ph_generation_fw], {self.scf_fw: self.ph_generation_fw},
                           metadata={'workflow_class': self.workflow_class,
                                     'workflow_module': self.workflow_module})

    @classmethod
    def from_factory(cls, structure, pseudos, kppa=None, ecut=None, pawecutdg=None, nband=None, accuracy="normal",
                     spin_mode="polarized", smearing="fermi_dirac:0.1 eV", charge=0.0, scf_algorithm=None,
                     shift_mode="Symmetric", ph_ngqpt=None, with_ddk=True, with_dde=True, with_bec=False,
                     scf_tol=None, ph_tol=None, ddk_tol=None, dde_tol=None, extra_abivars=None, decorators=None,
                     autoparal=False, spec=None, initialization_info=None):
        if extra_abivars is None:
            extra_abivars = {}
        if decorators is None:
            decorators = []
        if spec is None:
            spec = {}
        if initialization_info is None:
            initialization_info = {}

        extra_abivars_scf = dict(extra_abivars)
        extra_abivars_scf['tolwfr'] = scf_tol if scf_tol else 1.e-22
        scf_fact = ScfForPhononsFactory(structure=structure, pseudos=pseudos, kppa=kppa, ecut=ecut, pawecutdg=pawecutdg,
                                        nband=nband, accuracy=accuracy, spin_mode=spin_mode, smearing=smearing,
                                        charge=charge, scf_algorithm=scf_algorithm, shift_mode=shift_mode,
                                        extra_abivars=extra_abivars_scf, decorators=decorators)

        phonon_fact = PhononsFromGsFactory(ph_ngqpt=ph_ngqpt, with_ddk=with_ddk, with_dde=with_dde, with_bec=with_bec,
                                           ph_tol=ph_tol, ddk_tol=ddk_tol, dde_tol=dde_tol, extra_abivars=extra_abivars,
                                           decorators=decorators)

        return cls(scf_fact, phonon_fact, autoparal=autoparal, spec=spec, initialization_info=initialization_info)


class PhononFWWorkflow(AbstractFWWorkflow):
    workflow_class = 'PhononFWWorkflow'
    workflow_module = 'abiflows.fireworks.workflows.abinit_workflows'

    def __init__(self, scf_inp, phonon_factory, autoparal=False, spec=None, initialization_info=None):
        if spec is None:
            spec = {}
        if initialization_info is None:
            initialization_info = {}
        start_task_index = 1

        rf = self.get_reduced_formula(scf_inp)

        scf_task = ScfFWTask(scf_inp, is_autoparal=autoparal)

        spec = dict(spec)
        spec['initialization_info'] = initialization_info
        if autoparal:
            spec = self.set_short_single_core_to_spec(spec)
            start_task_index = 'autoparal'

        spec['wf_task_index'] = 'scf_' + str(start_task_index)


        self.scf_fw = Firework(scf_task, spec=spec, name=rf+"_"+scf_task.task_type)

        ph_generation_task = GeneratePhononFlowFWAbinitTask(phonon_factory, previous_task_type=scf_task.task_type,
                                                            with_autoparal=autoparal)

        spec['wf_task_index'] = 'gen_ph'

        self.ph_generation_fw = Firework(ph_generation_task, spec=spec, name=rf+"_gen_ph")

        self.wf = Workflow([self.scf_fw, self.ph_generation_fw], {self.scf_fw: self.ph_generation_fw},
                           metadata={'workflow_class': self.workflow_class,
                                     'workflow_module': self.workflow_module})

    @classmethod
    def from_factory(cls, structure, pseudos, kppa=None, ecut=None, pawecutdg=None, nband=None, accuracy="normal",
                     spin_mode="polarized", smearing="fermi_dirac:0.1 eV", charge=0.0, scf_algorithm=None,
                     shift_mode="Symmetric", ph_ngqpt=None, qpoints=None, qppa=None, with_ddk=True, with_dde=True,
                     with_bec=False, scf_tol=None, ph_tol=None, ddk_tol=None, dde_tol=None, wfq_tol=None,
                     qpoints_to_skip=None, extra_abivars=None, decorators=None, autoparal=False, spec=None, initialization_info=None):

        if extra_abivars is None:
            extra_abivars = {}
        if decorators is None:
            decorators = []
        if spec is None:
            spec = {}
        if initialization_info is None:
            initialization_info = {}

        if qppa is not None and (ph_ngqpt is not None or qpoints is not None):
            raise ValueError("qppa is incompatible with ph_ngqpt and qpoints")

        if qppa is not None:
            initialization_info['qppa'] = qppa
            ph_ngqpt = KSampling.automatic_density(structure, qppa, chksymbreak=0).kpts[0]

        initialization_info['ngqpt'] = ph_ngqpt
        initialization_info['qpoints'] = qpoints
        if 'kppa' not in initialization_info:
            initialization_info['kppa'] = kppa

        extra_abivars_scf = dict(extra_abivars)
        extra_abivars_scf['tolwfr'] = scf_tol if scf_tol else 1.e-22
        scf_fact = ScfForPhononsFactory(structure=structure, pseudos=pseudos, kppa=kppa, ecut=ecut, pawecutdg=pawecutdg,
                                        nband=nband, accuracy=accuracy, spin_mode=spin_mode, smearing=smearing,
                                        charge=charge, scf_algorithm=scf_algorithm, shift_mode=shift_mode,
                                        extra_abivars=extra_abivars_scf, decorators=decorators)

        phonon_fact = PhononsFromGsFactory(ph_ngqpt=ph_ngqpt, with_ddk=with_ddk, with_dde=with_dde, with_bec=with_bec,
                                           ph_tol=ph_tol, ddk_tol=ddk_tol, dde_tol=dde_tol, wfq_tol=wfq_tol,
                                           qpoints_to_skip=qpoints_to_skip, extra_abivars=extra_abivars,
                                           decorators=decorators)

        ph_wf = cls(scf_fact, phonon_fact, autoparal=autoparal, spec=spec, initialization_info=initialization_info)

        # if all the q points for a grid are calculated in this WF, add an anaddb task
        if ph_ngqpt and not qpoints_to_skip:
            ph_wf.add_anaddb_ph_bs_fw(Structure.as_structure(structure), ph_ngqpt)

        return ph_wf

    @classmethod
    def from_gs_input(cls, pseudos, gs_input, structure=None, ph_ngqpt=None, qpoints=None, qppa=None, with_ddk=True,
                      with_dde=True, with_bec=False, scf_tol=None, ph_tol=None, ddk_tol=None, dde_tol=None, wfq_tol=None,
                      qpoints_to_skip=None, extra_abivars=None, decorators=None, autoparal=False, spec=None, initialization_info=None):
        if extra_abivars is None:
            extra_abivars = {}
        if decorators is None:
            decorators = []
        if spec is None:
            spec = {}
        if initialization_info is None:
            initialization_info = {}

        if qppa is not None and (ph_ngqpt is not None or qpoints is not None):
            raise ValueError("qppa is incompatible with ph_ngqpt and qpoints")

        if qppa is not None:
            if structure is None:
                structure = gs_input.structure
            initialization_info['qppa'] = qppa
            ph_ngqpt = KSampling.automatic_density(structure, qppa, chksymbreak=0).kpts[0]

        initialization_info['ngqpt'] = ph_ngqpt
        initialization_info['qpoints'] = qpoints

        scf_inp = gs_input.deepcopy()
        if structure:
            scf_inp.set_structure(structure)
        scf_inp['tolwfr'] = scf_tol if scf_tol else 1.e-22
        scf_inp['chksymbreak'] = 1
        if not scf_inp.get('nbdbuf', 0):
            scf_inp['nbdbuf'] = 4
            scf_inp['nband'] = scf_inp['nband'] + 4
        abi_vars = get_abinit_variables()
        # remove relaxation variables in case gs_input is a relaxation
        for v in abi_vars.vars_with_section('varrlx'):
            scf_inp.pop(v.name, None)
        # remove parallelization variables in case gs_input is coming from a previous run with parallelization
        for v in abi_vars.vars_with_section('varpar'):
            scf_inp.pop(v.name, None)

        phonon_fact = PhononsFromGsFactory(ph_ngqpt=ph_ngqpt, with_ddk=with_ddk, with_dde=with_dde, with_bec=with_bec,
                                           ph_tol=ph_tol, ddk_tol=ddk_tol, dde_tol=dde_tol, wfq_tol=wfq_tol,
                                           qpoints_to_skip=qpoints_to_skip, extra_abivars=extra_abivars,
                                           decorators=decorators)

        ph_wf = cls(scf_inp, phonon_fact, autoparal=autoparal, spec=spec, initialization_info=initialization_info)

        # if all the q points for a grid are calculated in this WF, add an anaddb task
        if ph_ngqpt and not qpoints_to_skip:
            ph_wf.add_anaddb_ph_bs_fw(Structure.as_structure(structure), ph_ngqpt)

        return ph_wf

    def add_anaddb_ph_bs_fw(self, structure, ph_ngqpt, ndivsm=20, nqsmall=15):
        anaddb_input = AnaddbInput.phbands_and_dos(structure=structure, ngqpt=ph_ngqpt, ndivsm=ndivsm,nqsmall=nqsmall,
                                                   asr=2, chneut=1, dipdip=1, lo_to_splitting=True)
        anaddb_task = AnaDdbAbinitTask(anaddb_input, deps={MergeDdbAbinitTask.task_type: "DDB"})
        spec = dict(self.scf_fw.spec)
        spec['wf_task_index'] = 'anaddb'

        anaddb_fw = Firework(anaddb_task, spec=spec, name='anaddb')

        self.append_fw(anaddb_fw, short_single_spec=True)

    @classmethod
    def get_mongoengine_results(cls, wf):
        assert wf.metadata['workflow_class'] == cls.workflow_class
        assert wf.metadata['workflow_module'] == cls.workflow_module

        scf_index = 0
        ph_index = 0
        ddk_index = 0
        dde_index = 0
        wfq_index = 0

        anaddb_task = None
        for fw in wf.fws:
            task_index = fw.spec.get('wf_task_index', '')
            if task_index == 'anaddb':
                anaddb_launch = get_last_completed_launch(fw)
                anaddb_task = fw.tasks[-1]
                anaddb_task.set_workdir(workdir=anaddb_launch.launch_dir)
            elif task_index == 'mrgddb':
                mrgddb_launch = get_last_completed_launch(fw)
                mrgddb_task = fw.tasks[-1]
                mrgddb_task.set_workdir(workdir=mrgddb_launch.launch_dir)
            elif task_index.startswith('scf_') and not task_index.endswith('autoparal'):
                current_index = int(task_index.split('_')[-1])
                if current_index > scf_index:
                    scf_index = current_index
                    scf_fw = fw
            elif task_index.startswith('phonon_0') and not task_index.endswith('autoparal'):
                current_index = int(task_index.split('_')[-1])
                if current_index > ph_index:
                    ph_index = current_index
                    ph_fw = fw
            elif task_index.startswith('ddk_0') and not task_index.endswith('autoparal'):
                current_index = int(task_index.split('_')[-1])
                if current_index > ddk_index:
                    ddk_index = current_index
                    ddk_fw = fw
            elif task_index.startswith('dde_0') and not task_index.endswith('autoparal'):
                current_index = int(task_index.split('_')[-1])
                if current_index > dde_index:
                    dde_index = current_index
                    dde_fw = fw
            elif task_index.startswith('nscf_0') and not task_index.endswith('autoparal'):
                current_index = int(task_index.split('_')[-1])
                if current_index > wfq_index:
                    wfq_index = current_index
                    wfq_fw = fw

        scf_launch = get_last_completed_launch(scf_fw)
        scf_history = loadfn(os.path.join(scf_launch.launch_dir, 'history.json'))
        scf_task = scf_fw.tasks[-1]
        scf_task.set_workdir(workdir=scf_launch.launch_dir)

        document = PhononResult()

        gs_input = scf_history.get_events_by_types(TaskEvent.FINALIZED)[0].details['final_input']

        document.abinit_input.gs_input = gs_input.as_dict()
        document.abinit_input.set_abinit_basic_from_abinit_input(gs_input)

        structure = gs_input.structure
        document.abinit_output.structure = structure.as_dict()
        document.set_material_data_from_structure(structure)

        initialization_info = scf_history.get_events_by_types(TaskEvent.INITIALIZED)[0].details.get('initialization_info', {})
        document.mp_id = initialization_info.get('mp_id', None)

        document.relax_db = initialization_info['relax_db'].as_dict() if 'relax_db' in initialization_info else None
        document.relax_id = initialization_info.get('relax_id', None)

        document.abinit_input.ngqpt = initialization_info.get('ngqpt', None)
        document.abinit_input.qpoints = initialization_info.get('qpoints', None)
        document.abinit_input.qppa = initialization_info.get('qppa', None)
        document.abinit_input.kppa = initialization_info.get('kppa', None)

        document.abinit_input.pseudopotentials.set_pseudos_from_files_file(scf_task.files_file.path,
                                                                           len(structure.composition.elements))

        document.created_on = datetime.datetime.now()
        document.modified_on = datetime.datetime.now()

        document.set_dir_names_from_fws_wf(wf)

        with open(mrgddb_task.merged_ddb_path, "rt") as f:
            document.abinit_output.ddb.put(f)

        if ph_index > 0:
            ph_task = ph_fw.tasks[-1]
            document.abinit_input.phonon_input = ph_task.abiinput.as_dict()

        if ddk_index > 0:
            ddk_task = ddk_fw.tasks[-1]
            document.abinit_input.ddk_input = ddk_task.abiinput.as_dict()

        if dde_index > 0:
            dde_task = dde_fw.tasks[-1]
            document.abinit_input.dde_input = dde_task.abiinput.as_dict()

        if wfq_index > 0:
            wfq_task = wfq_fw.tasks[-1]
            document.abinit_input.wfq_input = wfq_task.abiinput.as_dict()

        if anaddb_task is not None:
            with open(anaddb_task.phbst_path, "rb") as f:
                document.abinit_output.phonon_bs.put(f)
            with open(anaddb_task.phdos_path, "rb") as f:
                document.abinit_output.phonon_dos.put(f)
            with open(anaddb_task.anaddb_nc_path, "rb") as f:
                document.abinit_output.anaddb_nc.put(f)

        document.fw_id = scf_fw.fw_id

        document.time_report = get_time_report_for_wf(wf).as_dict()

        with open(scf_task.gsr_path, "rb") as f:
            document.abinit_output.gs_gsr.put(f)

        with open(scf_task.output_file.path, "rt") as f:
            document.abinit_output.gs_outfile.put(f)

        return document


class PhononFullFWWorkflow(PhononFWWorkflow):
    """
    Same as PhononFWWorkflow, but the phonon FWs are generated immediately
    """
    workflow_class = 'PhononFullFWWorkflow'
    workflow_module = 'abiflows.fireworks.workflows.abinit_workflows'

    def __init__(self, scf_inp, phonon_factory, autoparal=False, spec=None, initialization_info=None):
        spec = spec or {}
        initialization_info = initialization_info or {}
        start_task_index = 1

        rf = self.get_reduced_formula(scf_inp)

        scf_task = ScfFWTask(scf_inp, is_autoparal=autoparal)

        spec = dict(spec)
        spec['initialization_info'] = initialization_info
        if autoparal:
            spec = self.set_short_single_core_to_spec(spec)
            start_task_index = 'autoparal'

        spec['wf_task_index'] = 'scf_' + str(start_task_index)


        self.scf_fw = Firework(scf_task, spec=spec, name=rf+"_"+scf_task.task_type)

        self.wf = Workflow([self.scf_fw],
                           metadata={'workflow_class': self.workflow_class,
                                     'workflow_module': self.workflow_module})
        self.wf.append_wf(self.generate_ph(phonon_factory, scf_inp, autoparal=autoparal, spec=spec,
                                           initialization_info=initialization_info, previous_task_type=ScfFWTask.task_type),
                          fw_ids=[self.scf_fw.fw_id])

    def get_fws(self, multi_inp, task_class, deps, spec, nscf_fws=None):
        formula = multi_inp[0].structure.composition.reduced_formula
        fws = []
        fw_deps = defaultdict(list)
        autoparal_spec = {}
        for i, inp in enumerate(multi_inp):
            spec = dict(spec)
            start_task_index = 1

            current_deps = dict(deps)
            parent_fw = None
            if nscf_fws:
                qpt = inp['qpt']
                for nscf_fw in nscf_fws:
                    if np.allclose(nscf_fw.tasks[0].abiinput['qpt'], qpt):
                        parent_fw = nscf_fw
                        current_deps[nscf_fw.tasks[0].task_type] = "WFQ"
                        break

            task = task_class(inp, deps=current_deps, is_autoparal=False)
            # this index is for the different task, each performing a different perturbation
            indexed_task_type = task_class.task_type + '_' + str(i)
            # this index is to index the restarts of the single task
            spec['wf_task_index'] = indexed_task_type + '_' + str(start_task_index)
            fw = Firework(task, spec=spec, name=(formula + '_' + indexed_task_type)[:15])
            fws.append(fw)
            if parent_fw is not None:
                fw_deps[parent_fw].append(fw)

        return fws, fw_deps

    def get_autoparal_fw(self, inp, task_type, deps, spec, nscf_fws=None):
        formula = inp.structure.composition.reduced_formula
        fw_deps = defaultdict(list)
        spec = dict(spec)

        current_deps = dict(deps)
        parent_fw = None
        if nscf_fws:
            qpt = inp['qpt']
            for nscf_fw in nscf_fws:
                if np.allclose(nscf_fw.tasks[0].abiinput['qpt'], qpt):
                    parent_fw = nscf_fw
                    current_deps[nscf_fw.tasks[0].task_type] = "WFQ"
                    break

        task = AutoparalTask(inp, deps=current_deps, forward_spec=True)
        # this index is for the different task, each performing a different perturbation
        indexed_task_type = AutoparalTask.task_type
        # this index is to index the restarts of the single task
        spec['wf_task_index'] = indexed_task_type + '_' + task_type
        fw = Firework(task, spec=spec, name=(formula + '_' + indexed_task_type)[:15])
        if parent_fw is not None:
            fw_deps[parent_fw].append(fw)

        return fw, fw_deps

    def generate_ph(self, ph_inputs, previous_input, autoparal, spec, initialization_info, previous_task_type):

        # Since everything is being generated here factories should be used to generate the AbinitInput

        if isinstance(previous_input, InputFactory):
            previous_input = previous_input.build_input()

        if isinstance(ph_inputs, InputFactory):
            initialization_info['input_factory'] = ph_inputs.as_dict()
            spec['initialization_info']['input_factory'] = ph_inputs.as_dict()
            ph_inputs = ph_inputs.build_input(previous_input)

        ph_q_pert_inputs = ph_inputs.filter_by_tags(PH_Q_PERT)
        ddk_inputs = ph_inputs.filter_by_tags(DDK)
        dde_inputs = ph_inputs.filter_by_tags(DDE)
        bec_inputs = ph_inputs.filter_by_tags(BEC)

        nscf_inputs = ph_inputs.filter_by_tags(NSCF)

        nscf_fws = []
        if nscf_inputs is not None:
            nscf_fws, nscf_fw_deps= self.get_fws(nscf_inputs, NscfFWTask,
                                                 {previous_task_type: "WFK", previous_task_type: "DEN"}, spec)

        ph_fws = []
        if ph_q_pert_inputs:
            ph_q_pert_inputs.set_vars(prtwf=-1)
            ph_fws, ph_fw_deps = self.get_fws(ph_q_pert_inputs, PhononTask, {previous_task_type: "WFK"}, spec,
                                              nscf_fws)

        ddk_fws = []
        if ddk_inputs:
            ddk_fws, ddk_fw_deps = self.get_fws(ddk_inputs, DdkTask, {previous_task_type: "WFK"}, spec)

        dde_fws = []
        if dde_inputs:
            dde_inputs.set_vars(prtwf=-1)
            dde_fws, dde_fw_deps = self.get_fws(dde_inputs, DdeTask,
                                                {previous_task_type: "WFK", DdkTask.task_type: "DDK"}, spec)

        bec_fws = []
        if bec_inputs:
            bec_inputs.set_vars(prtwf=-1)
            bec_fws, bec_fw_deps = self.get_fws(bec_inputs, BecTask,
                                                {previous_task_type: "WFK", DdkTask.task_type: "DDK"}, spec)

        mrgddb_spec = dict(spec)
        mrgddb_spec['wf_task_index'] = 'mrgddb'
        #FIXME import here to avoid circular imports.
        from abiflows.fireworks.utils.fw_utils import get_short_single_core_spec
        qadapter_spec = self.set_short_single_core_to_spec(mrgddb_spec)
        mrgddb_spec['mpi_ncpus'] = 1
        # Set a higher priority to favour the end of the WF
        #TODO improve the handling of the priorities
        mrgddb_spec['_priority'] = 10
        num_ddbs_to_be_merged = len(ph_fws) + len(dde_fws) + len(bec_fws)
        mrgddb_fw = Firework(MergeDdbAbinitTask(num_ddbs=num_ddbs_to_be_merged, delete_source_ddbs=False), spec=mrgddb_spec,
                             name=ph_inputs[0].structure.composition.reduced_formula+'_mergeddb')

        fws_deps = {}

        autoparal_fws = []
        if autoparal:
            # add an AutoparalTask for each type and relative dependencies
            dfpt_autoparal_fw = self.get_autoparal_fw(ph_q_pert_inputs[0], 'dfpt', {previous_task_type: "WFK"}, spec,
                                                      nscf_fws)[0]
            autoparal_fws.append(dfpt_autoparal_fw)

            fws_deps[dfpt_autoparal_fw] = ph_fws + ddk_fws + dde_fws + bec_fws

            if nscf_fws:
                nscf_autoparal_fw = self.get_autoparal_fw(nscf_inputs[0], 'nscf',
                                                          {previous_task_type: "WFK", previous_task_type: "DEN"}, spec)[0]
                fws_deps[nscf_autoparal_fw] = nscf_fws
                autoparal_fws.append(nscf_autoparal_fw)

        if ddk_fws:
            for ddk_fw in ddk_fws:
                if dde_fws:
                    fws_deps[ddk_fw] = dde_fws
                if bec_fws:
                    fws_deps[ddk_fw] = bec_fws

        ddb_fws = dde_fws + ph_fws + bec_fws
        #TODO pass all the tasks to the MergeDdbTask for logging or easier retrieve of the DDK?
        for ddb_fw in ddb_fws:
            fws_deps[ddb_fw] = mrgddb_fw

        total_list_fws = ddb_fws+ddk_fws+[mrgddb_fw] + nscf_fws + autoparal_fws

        fws_deps.update(ph_fw_deps)

        ph_wf = Workflow(total_list_fws, fws_deps)

        return ph_wf


class PiezoElasticFWWorkflow(AbstractFWWorkflow):
    workflow_class = 'PiezoElasticFWWorkflow'
    workflow_module = 'abiflows.fireworks.workflows.abinit_workflows'

    def __init__(self, scf_inp, ddk_inp, rf_inp, autoparal=False, spec=None, initialization_info=None):
        if spec is None:
            spec = {}
        if initialization_info is None:
            initialization_info = {}
        rf = self.get_reduced_formula(scf_inp)

        scf_task = ScfFWTask(scf_inp, is_autoparal=autoparal)

        spec = dict(spec)
        spec['initialization_info'] = initialization_info
        if autoparal:
            spec = self.set_short_single_core_to_spec(spec)

        self.scf_fw = Firework(scf_task, spec=spec, name=rf+"_"+scf_task.task_type)

        ddk_task = DdkTask(ddk_inp, is_autoparal=autoparal, deps={scf_task.task_type: 'WFK'})

        ddk_fw_name = rf+ddk_task.task_type
        ddk_fw_name = ddk_fw_name[:8]
        self.ddk_fw = Firework(ddk_task, spec=spec, name=ddk_fw_name)

        rf_task = StrainPertTask(rf_inp, is_autoparal=autoparal, deps={scf_task.task_type: 'WFK', ddk_task.task_type: 'DDK'})

        rf_fw_name = rf+rf_task.task_type
        rf_fw_name = rf_fw_name[:8]
        self.rf_fw = Firework(rf_task, spec=spec, name=rf_fw_name)

        self.wf = Workflow(fireworks=[self.scf_fw, self.ddk_fw, self.rf_fw],
                           links_dict={self.scf_fw: self.ddk_fw, self.ddk_fw: self.rf_fw},
                           metadata={'workflow_class': self.workflow_class,
                                     'workflow_module': self.workflow_module})

        self.add_anaddb_task(scf_inp.structure)

    def add_anaddb_task(self, structure):
        spec = self.set_short_single_core_to_spec()
        anaddb_task = AnaDdbAbinitTask(AnaddbInput.piezo_elastic(structure))
        anaddb_fw = Firework([anaddb_task],
                             spec=spec,
                             name='anaddb')
        append_fw_to_wf(anaddb_fw, self.wf)

    def add_mrgddb_task(self, structure):
        spec = self.set_short_single_core_to_spec()
        spec['ddb_files_task_types'] = ['scf', 'strain_pert']
        mrgddb_task = MergeDdbAbinitTask()
        mrgddb_fw = Firework([mrgddb_task], spec=spec, name='mrgddb')
        append_fw_to_wf(mrgddb_fw, self.wf)

    @classmethod
    def get_elastic_tensor_and_history(cls, wf):
        assert wf.metadata['workflow_class'] == cls.workflow_class
        assert wf.metadata['workflow_module'] == cls.workflow_module

        final_fw_id = None
        for fw_id, fw in wf.id_fw.items():
            if fw.name == 'anaddb':
                final_fw_id = fw_id
        if final_fw_id is None:
            raise RuntimeError('Final anaddb task not found ...')
        myfw = wf.id_fw[final_fw_id]
        #TODO add a check on the state of the launches
        last_launch = (myfw.archived_launches + myfw.launches)[-1]
        #TODO add a cycle to find the instance of AbiFireTask?
        myfw.tasks[-1].set_workdir(workdir=last_launch.launch_dir)
        elastic_tensor = myfw.tasks[-1].get_elastic_tensor()
        history = loadfn(os.path.join(last_launch.launch_dir, 'history.json'))

        return {'elastic_properties': elastic_tensor.extended_dict(), 'history': history}

    @classmethod
    def get_all_elastic_tensors(cls, wf):
        assert wf.metadata['workflow_class'] == cls.workflow_class
        assert wf.metadata['workflow_module'] == cls.workflow_module

        final_fw_id = None
        for fw_id, fw in wf.id_fw.items():
            if fw.name == 'anaddb':
                final_fw_id = fw_id
        if final_fw_id is None:
            raise RuntimeError('Final anaddb task not found ...')
        myfw = wf.id_fw[final_fw_id]
        #TODO add a check on the state of the launches
        last_launch = (myfw.archived_launches + myfw.launches)[-1]
        #TODO add a cycle to find the instance of AbiFireTask?
        myfw.tasks[-1].set_workdir(workdir=last_launch.launch_dir)
        elastic_tensor = myfw.tasks[-1].get_elastic_tensor()
        history = loadfn(os.path.join(last_launch.launch_dir, 'history.json'))

        return {'elastic_properties': elastic_tensor.extended_dict(), 'history': history}

    @classmethod
    def from_factory(cls):
        raise NotImplemented('from factory method not yet implemented for piezoelasticworkflow')


class PiezoElasticFWWorkflowSRCOld(AbstractFWWorkflow):
    workflow_class = 'PiezoElasticFWWorkflowSRC'
    workflow_module = 'abiflows.fireworks.workflows.abinit_workflows'

    STANDARD_HANDLERS = {'_all': [MemoryHandler(), WalltimeHandler()]}
    STANDARD_VALIDATORS = {'_all': []}

    def __init__(self, scf_inp_ibz, ddk_inp, rf_inp, spec=None, initialization_info=None,
                 handlers=None, validators=None, ddk_split=False, rf_split=False):
        if spec is None:
            spec = {}
        if initialization_info is None:
            initialization_info = {}
        if handlers is None:
            handlers = self.STANDARD_HANDLERS
        if validators is None:
            validators = self.STANDARD_VALIDATORS

        fws = []
        links_dict = {}

        if 'queue_adapter_update' in initialization_info:
            queue_adapter_update = initialization_info['queue_adapter_update']
        else:
            queue_adapter_update = None

        # If handlers are passed as a list, they should be applied on all task_types
        if isinstance(handlers, (list, tuple)):
            handlers = {'_all': handlers}
        # If validators are passed as a list, they should be applied on all task_types
        if isinstance(validators, (list, tuple)):
            validators = {'_all': validators}

        #1. First SCF run in the irreducible Brillouin Zone
        SRC_scf_ibz_fws = createSRCFireworksOld(task_class=ScfFWTask, task_input=scf_inp_ibz, SRC_spec=spec,
                                                initialization_info=initialization_info,
                                                wf_task_index_prefix='scfibz', task_type='scfibz',
                                                handlers=handlers['_all'], validators=validators['_all'],
                                                queue_adapter_update=queue_adapter_update)
        fws.extend(SRC_scf_ibz_fws['fws'])
        links_dict_update(links_dict=links_dict, links_update=SRC_scf_ibz_fws['links_dict'])

        #2. Second SCF run in the full Brillouin Zone with kptopt 3 in order to allow merging 1st derivative DDB's with
        #2nd derivative DDB's from the DFPT RF run
        scf_inp_fbz = scf_inp_ibz.deepcopy()
        scf_inp_fbz['kptopt'] = 2
        SRC_scf_fbz_fws = createSRCFireworksOld(task_class=ScfFWTask, task_input=scf_inp_fbz, SRC_spec=spec,
                                                initialization_info=initialization_info,
                                                wf_task_index_prefix='scffbz', task_type='scffbz',
                                                handlers=handlers['_all'], validators=validators['_all'],
                                                deps={SRC_scf_ibz_fws['run_fw'].tasks[0].task_type: ['DEN', 'WFK']},
                                                queue_adapter_update=queue_adapter_update)
        fws.extend(SRC_scf_fbz_fws['fws'])
        links_dict_update(links_dict=links_dict, links_update=SRC_scf_fbz_fws['links_dict'])
        #Link with previous SCF
        links_dict_update(links_dict=links_dict,
                          links_update={SRC_scf_ibz_fws['check_fw'].fw_id: SRC_scf_fbz_fws['setup_fw'].fw_id})

        #3. DDK calculation
        if ddk_split:
            raise NotImplementedError('Split Ddk to be implemented in PiezoElasticWorkflow ...')
        else:
            SRC_ddk_fws = createSRCFireworksOld(task_class=DdkTask, task_input=ddk_inp, SRC_spec=spec,
                                                initialization_info=initialization_info,
                                                wf_task_index_prefix='ddk',
                                                handlers=handlers['_all'], validators=validators['_all'],
                                                deps={SRC_scf_ibz_fws['run_fw'].tasks[0].task_type: 'WFK'},
                                                queue_adapter_update=queue_adapter_update)
            fws.extend(SRC_ddk_fws['fws'])
            links_dict_update(links_dict=links_dict, links_update=SRC_ddk_fws['links_dict'])
            #Link with the IBZ SCF run
            links_dict_update(links_dict=links_dict,
                              links_update={SRC_scf_ibz_fws['check_fw'].fw_id: SRC_ddk_fws['setup_fw'].fw_id})

        #4. Response-Function calculation(s) of the elastic constants
        if rf_split:
            rf_ddb_source_task_type = 'mrgddb-strains'
            scf_task_type = SRC_scf_ibz_fws['run_fw'].tasks[0].task_type
            ddk_task_type = SRC_ddk_fws['run_fw'].tasks[0].task_type
            gen_task = GeneratePiezoElasticFlowFWAbinitTask(previous_scf_task_type=scf_task_type,
                                                            previous_ddk_task_type=ddk_task_type,
                                                            handlers=handlers, validators=validators,
                                                            mrgddb_task_type=rf_ddb_source_task_type)
            genrfstrains_spec = set_short_single_core_to_spec(spec)
            gen_fw = Firework([gen_task], spec=genrfstrains_spec, name='gen-piezo-elast')
            fws.append(gen_fw)
            links_dict_update(links_dict=links_dict,
                              links_update={SRC_scf_ibz_fws['check_fw'].fw_id: gen_fw.fw_id,
                                            SRC_ddk_fws['check_fw'].fw_id: gen_fw.fw_id})
            rf_ddb_src_fw = gen_fw
        else:
            SRC_rf_fws = createSRCFireworksOld(task_class=StrainPertTask, task_input=rf_inp, SRC_spec=spec,
                                               initialization_info=initialization_info,
                                               wf_task_index_prefix='rf',
                                               handlers=handlers['_all'], validators=validators['_all'],
                                               deps={SRC_scf_ibz_fws['run_fw'].tasks[0].task_type: 'WFK',
                                                     SRC_ddk_fws['run_fw'].tasks[0].task_type: 'DDK'},
                                               queue_adapter_update=queue_adapter_update)
            fws.extend(SRC_rf_fws['fws'])
            links_dict_update(links_dict=links_dict, links_update=SRC_rf_fws['links_dict'])
            #Link with the IBZ SCF run and the DDK run
            links_dict_update(links_dict=links_dict,
                              links_update={SRC_scf_ibz_fws['check_fw'].fw_id: SRC_rf_fws['setup_fw'].fw_id,
                                            SRC_ddk_fws['check_fw'].fw_id: SRC_rf_fws['setup_fw'].fw_id})
            rf_ddb_source_task_type = SRC_rf_fws['run_fw'].tasks[0].task_type
            rf_ddb_src_fw = SRC_rf_fws['check_fw']

        #5. Merge DDB files from response function (second derivatives for the elastic constants) and from the
        # SCF run on the full Brillouin zone (first derivatives for the stress tensor, to be used for the
        # stress-corrected elastic constants)
        mrgddb_task = MergeDdbAbinitTask(ddb_source_task_types=[rf_ddb_source_task_type,
                                                                SRC_scf_fbz_fws['run_fw'].tasks[0].task_type],
                                         delete_source_ddbs=False, num_ddbs=2)
        mrgddb_spec = set_short_single_core_to_spec(spec)
        mrgddb_fw = Firework(tasks=[mrgddb_task], spec=mrgddb_spec, name='mrgddb')
        fws.append(mrgddb_fw)
        links_dict_update(links_dict=links_dict,
                          links_update={rf_ddb_src_fw.fw_id: mrgddb_fw.fw_id,
                                        SRC_scf_fbz_fws['check_fw'].fw_id: mrgddb_fw.fw_id})

        #6. Anaddb task to get elastic constants based on the RF run (no stress correction)
        anaddb_tag = 'anaddb-piezo-elast'
        spec = set_short_single_core_to_spec(spec)
        anaddb_task = AnaDdbAbinitTask(AnaddbInput.piezo_elastic(structure=scf_inp_ibz.structure,
                                                                 stress_correction=False),
                                       deps={rf_ddb_source_task_type: ['DDB']},
                                       task_type=anaddb_tag)
        anaddb_fw = Firework([anaddb_task],
                             spec=spec,
                             name=anaddb_tag)
        fws.append(anaddb_fw)
        links_dict_update(links_dict=links_dict,
                          links_update={rf_ddb_src_fw.fw_id: anaddb_fw.fw_id})

        #7. Anaddb task to get elastic constants based on the RF run and the SCF run (with stress correction)
        anaddb_tag = 'anaddb-piezo-elast-stress-corrected'
        spec = set_short_single_core_to_spec(spec)
        anaddb_stress_task = AnaDdbAbinitTask(AnaddbInput.piezo_elastic(structure=scf_inp_ibz.structure,
                                                                        stress_correction=True),
                                              deps={mrgddb_task.task_type: ['DDB']},
                                              task_type=anaddb_tag)
        anaddb_stress_fw = Firework([anaddb_stress_task],
                                    spec=spec,
                                    name=anaddb_tag)
        fws.append(anaddb_stress_fw)
        links_dict_update(links_dict=links_dict,
                          links_update={mrgddb_fw.fw_id: anaddb_stress_fw.fw_id})

        self.wf = Workflow(fireworks=fws,
                           links_dict=links_dict,
                           metadata={'workflow_class': self.workflow_class,
                                     'workflow_module': self.workflow_module})

    @classmethod
    def get_all_elastic_tensors(cls, wf):
        assert wf.metadata['workflow_class'] == cls.workflow_class
        assert wf.metadata['workflow_module'] == cls.workflow_module

        anaddb_no_stress_id = None
        anaddb_stress_id = None
        for fw_id, fw in wf.id_fw.items():
            if fw.name == 'anaddb-piezo-elast':
                anaddb_no_stress_id = fw_id
            if fw.name == 'anaddb-piezo-elast-stress-corrected':
                anaddb_stress_id = fw_id
        if anaddb_no_stress_id is None or anaddb_stress_id is None:
            raise RuntimeError('Final anaddb tasks not found ...')
        myfw_nostress = wf.id_fw[anaddb_no_stress_id]
        last_launch_nostress = (myfw_nostress.archived_launches + myfw_nostress.launches)[-1]
        myfw_nostress.tasks[-1].set_workdir(workdir=last_launch_nostress.launch_dir)

        myfw_stress = wf.id_fw[anaddb_stress_id]
        last_launch_stress = (myfw_stress.archived_launches + myfw_stress.launches)[-1]
        myfw_stress.tasks[-1].set_workdir(workdir=last_launch_stress.launch_dir)

        ec_nostress_clamped = myfw_nostress.tasks[-1].get_elastic_tensor(tensor_type='clamped_ion')
        ec_nostress_relaxed = myfw_nostress.tasks[-1].get_elastic_tensor(tensor_type='relaxed_ion')
        ec_stress_relaxed = myfw_stress.tasks[-1].get_elastic_tensor(tensor_type='relaxed_ion_stress_corrected')

        ec_dicts = {'clamped_ion': ec_nostress_clamped.extended_dict(),
                    'relaxed_ion': ec_nostress_relaxed.extended_dict(),
                    'relaxed_ion_stress_corrected': ec_stress_relaxed.extended_dict()}

        return {'elastic_properties': ec_dicts}

    @classmethod
    def from_factory(cls):
        raise NotImplemented('from factory method not yet implemented for piezoelasticworkflow')


class PiezoElasticFWWorkflowSRC(AbstractFWWorkflow):
    workflow_class = 'PiezoElasticFWWorkflowSRC'
    workflow_module = 'abiflows.fireworks.workflows.abinit_workflows'


    def __init__(self, scf_inp_ibz, ddk_inp, rf_inp, spec=None, initialization_info=None,
                 ddk_split=False, rf_split=False, additional_controllers=None, additional_input_vars=None,
                 allow_parallel_perturbations=True, do_ddk=True, do_phonons=True):
        if spec is None:
            spec = {}
        if initialization_info is None:
            initialization_info = {}

        fws = []
        links_dict = {}

        if additional_controllers is None:
            additional_controllers = [WalltimeController(), MemoryController()]
        else:
            additional_controllers = additional_controllers

        if additional_input_vars is None:
            additional_input_vars = {}

        # Dependencies for the ngfft grid (for some reason, the fft grid can change between SCF and nSCF runs
        # even when all other parameters are the same ...)
        ngfft_deps = ['#outnc.ngfft']
        if scf_inp_ibz.ispaw:
            ngfft_deps.append('#outnc.ngfftdg')

        scf_inp_ibz.set_vars(additional_input_vars)
        if do_ddk:
            ddk_inp.set_vars(additional_input_vars)
        rf_inp.set_vars(additional_input_vars)
        if not do_ddk:
            rf_inp.set_vars(irdddk=0)

        #1. SCF run in the irreducible Brillouin Zone
        scf_helper = ScfTaskHelper()
        scf_controllers = [AbinitController.from_helper(scf_helper)]
        scf_controllers.extend(additional_controllers)
        scf_control_procedure = ControlProcedure(controllers=scf_controllers)
        setup_scf_task = AbinitSetupTask(abiinput=scf_inp_ibz, task_helper=scf_helper, pass_input=True)
        run_scf_task = AbinitRunTask(control_procedure=scf_control_procedure, task_helper=scf_helper,
                                     task_type='scfibz')
        control_scf_task = AbinitControlTask(control_procedure=scf_control_procedure, task_helper=scf_helper)

        scf_fws = createSRCFireworks(setup_task=setup_scf_task, run_task=run_scf_task, control_task=control_scf_task,
                                     spec=spec, initialization_info=initialization_info)

        fws.extend(scf_fws['fws'])
        links_dict_update(links_dict=links_dict, links_update=scf_fws['links_dict'])

        #2. nSCF run in the full Brillouin Zone with kptopt 2
        nscf_helper = NscfTaskHelper()
        nscf_controllers = [AbinitController.from_helper(nscf_helper)]
        nscf_controllers.extend(additional_controllers)
        nscf_control_procedure = ControlProcedure(controllers=nscf_controllers)
        nscf_inp_fbz = scf_inp_ibz.deepcopy()
        nscf_inp_fbz.set_vars({'tolwfr': 1.0e-20,
                               'kptopt': 3,
                               'iscf': -2,
                               'istwfk': '*1'})
        # Adding buffer to help convergence ...
        if 'nbdbuf' not in nscf_inp_fbz:
            nbdbuf = max(int(0.1*nscf_inp_fbz['nband']), 4)
            nscf_inp_fbz.set_vars(nband=nscf_inp_fbz['nband']+nbdbuf, nbdbuf=nbdbuf)
        nscffbz_deps = {run_scf_task.task_type: ['DEN']}
        nscffbz_deps[run_scf_task.task_type].extend(ngfft_deps)
        nscf_inp_fbz['prtvol'] = 10
        setup_nscffbz_task = AbinitSetupTask(abiinput=nscf_inp_fbz, task_helper=nscf_helper,
                                             deps=nscffbz_deps, pass_input=True)
        run_nscffbz_task = AbinitRunTask(control_procedure=nscf_control_procedure, task_helper=nscf_helper,
                                         task_type='nscffbz')
        control_nscffbz_task = AbinitControlTask(control_procedure=nscf_control_procedure, task_helper=nscf_helper)

        nscffbz_fws = createSRCFireworks(setup_task=setup_nscffbz_task, run_task=run_nscffbz_task,
                                         control_task=control_nscffbz_task,
                                         spec=spec, initialization_info=initialization_info)

        fws.extend(nscffbz_fws['fws'])
        links_dict_update(links_dict=links_dict, links_update=nscffbz_fws['links_dict'])
        #Link with the IBZ SCF run
        links_dict_update(links_dict=links_dict,
                          links_update={scf_fws['control_fw'].fw_id: nscffbz_fws['setup_fw'].fw_id})

        #3. DDK calculation
        if do_ddk:
            if ddk_split:
                raise NotImplementedError('Split Ddk to be implemented in PiezoElasticWorkflow ...')
            else:
                ddk_helper = DdkTaskHelper()
                ddk_controllers = [AbinitController.from_helper(ddk_helper)]
                ddk_controllers.extend(additional_controllers)
                ddk_control_procedure = ControlProcedure(controllers=ddk_controllers)
                ddk_inp.set_vars({'kptopt': 3})
                ddk_deps = {run_nscffbz_task.task_type: ['WFK']}
                ddk_deps[run_nscffbz_task.task_type].extend(ngfft_deps)
                setup_ddk_task = AbinitSetupTask(abiinput=ddk_inp, task_helper=ddk_helper,
                                                 deps=ddk_deps)
                run_ddk_task = AbinitRunTask(control_procedure=ddk_control_procedure, task_helper=ddk_helper,
                                             task_type='ddk')
                control_ddk_task = AbinitControlTask(control_procedure=ddk_control_procedure, task_helper=ddk_helper)

                ddk_fws = createSRCFireworks(setup_task=setup_ddk_task, run_task=run_ddk_task,
                                             control_task=control_ddk_task,
                                             spec=spec, initialization_info=initialization_info)

                fws.extend(ddk_fws['fws'])
                links_dict_update(links_dict=links_dict, links_update=ddk_fws['links_dict'])
                #Link with the FBZ nSCF run
                links_dict_update(links_dict=links_dict,
                                  links_update={nscffbz_fws['control_fw'].fw_id: ddk_fws['setup_fw'].fw_id})

        #4. Response-Function calculation(s) of the elastic constants
        rf_ddb_source_task_type = 'mrgddb-strains'
        rf_tolvar, value = rf_inp.scf_tolvar
        rf_tol = {rf_tolvar: value}
        rf_deps = {run_nscffbz_task.task_type: ['WFK']}
        if do_ddk:
            rf_deps[run_ddk_task.task_type] = ['DDK']
            previous_ddk_task_type = run_ddk_task.task_type
        else:
            previous_ddk_task_type = None
        rf_deps[run_nscffbz_task.task_type].extend(ngfft_deps)
        gen_task = GeneratePiezoElasticFlowFWSRCAbinitTask(previous_scf_task_type=run_nscffbz_task.task_type,
                                                           previous_ddk_task_type=previous_ddk_task_type,
                                                           mrgddb_task_type=rf_ddb_source_task_type,
                                                           additional_controllers=additional_controllers,
                                                           rf_tol=rf_tol, additional_input_vars=additional_input_vars,
                                                           rf_deps=rf_deps,
                                                           allow_parallel_perturbations=allow_parallel_perturbations,
                                                           do_phonons=do_phonons)
        genrfstrains_spec = set_short_single_core_to_spec(spec)
        gen_fw = Firework([gen_task], spec=genrfstrains_spec, name='gen-piezo-elast')
        fws.append(gen_fw)
        linkupdate = {nscffbz_fws['control_fw'].fw_id: gen_fw.fw_id}
        if do_ddk:
            linkupdate[ddk_fws['control_fw'].fw_id] = gen_fw.fw_id
        links_dict_update(links_dict=links_dict,
                          links_update=linkupdate)

        rf_ddb_src_fw = gen_fw

        #5. Merge DDB files from response function (second derivatives for the elastic constants) and from the
        # SCF run on the full Brillouin zone (first derivatives for the stress tensor, to be used for the
        # stress-corrected elastic constants)
        mrgddb_task = MergeDdbAbinitTask(ddb_source_task_types=[rf_ddb_source_task_type,
                                                                run_scf_task.task_type],
                                         delete_source_ddbs=False, num_ddbs=2)
        mrgddb_spec = set_short_single_core_to_spec(spec)
        if scf_inp_ibz.ispaw:
            mrgddb_spec['PAW_datasets_description_correction'] = 'yes'
        mrgddb_fw = Firework(tasks=[mrgddb_task], spec=mrgddb_spec, name='mrgddb')
        fws.append(mrgddb_fw)
        links_dict_update(links_dict=links_dict,
                          links_update={rf_ddb_src_fw.fw_id: mrgddb_fw.fw_id,
                                        scf_fws['control_fw'].fw_id: mrgddb_fw.fw_id})

        #6. Anaddb task to get elastic constants based on the RF run (no stress correction)
        anaddb_tag = 'anaddb-piezo-elast'
        spec = set_short_single_core_to_spec(spec)
        anaddb_task = AnaDdbAbinitTask(AnaddbInput.piezo_elastic(structure=scf_inp_ibz.structure,
                                                                 stress_correction=False),
                                       deps={rf_ddb_source_task_type: ['DDB']},
                                       task_type=anaddb_tag)
        anaddb_fw = Firework([anaddb_task],
                             spec=spec,
                             name=anaddb_tag)
        fws.append(anaddb_fw)
        links_dict_update(links_dict=links_dict,
                          links_update={rf_ddb_src_fw.fw_id: anaddb_fw.fw_id})

        #7. Anaddb task to get elastic constants based on the RF run and the SCF run (with stress correction)
        anaddb_tag = 'anaddb-piezo-elast-stress-corrected'
        spec = set_short_single_core_to_spec(spec)
        anaddb_stress_task = AnaDdbAbinitTask(AnaddbInput.piezo_elastic(structure=scf_inp_ibz.structure,
                                                                        stress_correction=True),
                                              deps={mrgddb_task.task_type: ['DDB']},
                                              task_type=anaddb_tag)
        anaddb_stress_fw = Firework([anaddb_stress_task],
                                    spec=spec,
                                    name=anaddb_tag)
        fws.append(anaddb_stress_fw)
        links_dict_update(links_dict=links_dict,
                          links_update={mrgddb_fw.fw_id: anaddb_stress_fw.fw_id})

        self.wf = Workflow(fireworks=fws,
                           links_dict=links_dict,
                           metadata={'workflow_class': self.workflow_class,
                                     'workflow_module': self.workflow_module})

    def add_anaddb_task(self, structure):
        spec = self.set_short_single_core_to_spec()
        anaddb_task = AnaDdbAbinitTask(AnaddbInput.piezo_elastic(structure))
        anaddb_fw = Firework([anaddb_task],
                             spec=spec,
                             name='anaddb')
        append_fw_to_wf(anaddb_fw, self.wf)

    @classmethod
    def get_all_elastic_tensors(cls, wf):
        assert wf.metadata['workflow_class'] == cls.workflow_class
        assert wf.metadata['workflow_module'] == cls.workflow_module

        anaddb_no_stress_id = None
        anaddb_stress_id = None
        for fw_id, fw in wf.id_fw.items():
            if fw.name == 'anaddb-piezo-elast':
                anaddb_no_stress_id = fw_id
            if fw.name == 'anaddb-piezo-elast-stress-corrected':
                anaddb_stress_id = fw_id
        if anaddb_no_stress_id is None or anaddb_stress_id is None:
            raise RuntimeError('Final anaddb tasks not found ...')
        myfw_nostress = wf.id_fw[anaddb_no_stress_id]
        last_launch_nostress = (myfw_nostress.archived_launches + myfw_nostress.launches)[-1]
        myfw_nostress.tasks[-1].set_workdir(workdir=last_launch_nostress.launch_dir)

        myfw_stress = wf.id_fw[anaddb_stress_id]
        last_launch_stress = (myfw_stress.archived_launches + myfw_stress.launches)[-1]
        myfw_stress.tasks[-1].set_workdir(workdir=last_launch_stress.launch_dir)

        ec_nostress_clamped = myfw_nostress.tasks[-1].get_elastic_tensor(tensor_type='clamped_ion')
        ec_nostress_relaxed = myfw_nostress.tasks[-1].get_elastic_tensor(tensor_type='relaxed_ion')
        ec_stress_relaxed = myfw_stress.tasks[-1].get_elastic_tensor(tensor_type='relaxed_ion_stress_corrected')

        ec_dicts = {'clamped_ion': ec_nostress_clamped.extended_dict(),
                    'relaxed_ion': ec_nostress_relaxed.extended_dict(),
                    'relaxed_ion_stress_corrected': ec_stress_relaxed.extended_dict()}

        return {'elastic_properties': ec_dicts}

    @classmethod
    def from_factory(cls):
        raise NotImplemented('from factory method not yet implemented for piezoelasticworkflow')
