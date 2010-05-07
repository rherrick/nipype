"""The fsl module provides classes for interfacing with the `FSL
<http://www.fmrib.ox.ac.uk/fsl/index.html>`_ command line tools.  This
was written to work with FSL version 4.1.4.

Examples
--------
See the docstrings of the individual classes for examples.

"""

import os
from copy import deepcopy
from glob import glob
import warnings
from shutil import rmtree

from nipype.interfaces.fsl.base import (FSLCommand, FSLInfo, FSLTraitedSpec,
                                        NEW_FSLCommand)
from nipype.interfaces.base import (Bunch, Interface, load_template,
                                    InterfaceResult, File, Directory, traits,
                                    TraitedSpec,
                                    NEW_BaseInterface,
                                    InputMultiPath, OutputMultiPath)
from nipype.utils.filemanip import (list_to_filename, filename_to_list,
                                    loadflat)
from nipype.utils.docparse import get_doc
from nipype.externals.pynifti import load
from nipype.utils.misc import isdefined
from nipype.interfaces.traits import Directory

warn = warnings.warn
warnings.filterwarnings('always', category=UserWarning)

class Level1DesignInputSpec(TraitedSpec):
    interscan_interval = traits.Float(mandatory=True,
                desc='Interscan  interval (in secs)')
    session_info = File(exists=True, mandatory=True,
                desc='File containing session specific information generated by ``modelgen.SpecifyModel``')
    bases = traits.Either(traits.Dict('dgamma', traits.Dict('derivs', traits.Bool)),
                          traits.Dict('gamma', traits.Dict('derivs', traits.Bool)),
                          mandatory=True,
                          desc='name of basis function and options')
    model_serial_correlations = traits.Enum('AR(1)', 'none',
        desc="""Option to model serial correlations using an
            autoregressive estimator. Setting this option is only
            useful in the context of the fsf file. You need to repeat
            this option for FilmGLS""")
    contrasts = traits.List(
        traits.Either(traits.Tuple(traits.Str,
                                   traits.Enum('T'),
                                   traits.List(traits.Str),
                                   traits.List(traits.Float)),
                      traits.Tuple(traits.Str,
                                   traits.Enum('T'),
                                   traits.List(traits.Str),
                                   traits.List(traits.Float),
                                   traits.List(traits.Float)),
                      traits.Tuple(traits.Str,
                                   traits.Enum('F'),
                                   traits.List(traits.Either(traits.Tuple(traits.Str,
                                                                          traits.Enum('T'),
                                                                          traits.List(traits.Str),
                                                                          traits.List(traits.Float)),
                                                             traits.Tuple(traits.Str,
                                                                          traits.Enum('T'),
                                                                          traits.List(traits.Str),
                                                                          traits.List(traits.Float),
                                                                          traits.List(traits.Float)))))),
        desc="""List of contrasts with each contrast being a list of the form -
    [('name', 'stat', [condition list], [weight list], [session list])]. if
    session list is None or not provided, all sessions are used. For F
    contrasts, the condition list should contain previously defined
    T-contrasts.""")
    register = traits.Bool(requires=['reg_dof'],
        desc='Run registration at the end of session specific analysis.')
    reg_image = File(exists=True, desc='image volume to register to')
    # MNI152_T1_2mm_brain.nii.gz
    reg_dof = traits.Enum(3,6,9,12,
                          desc='registration degrees of freedom')

class Level1DesignOutputSpec(TraitedSpec):
    fsf_files = OutputMultiPath(File(exists=True),
                     desc='FSL feat specification files')
    ev_files = OutputMultiPath(File(exists=True),
                     desc='condition information files')

class Level1Design(NEW_BaseInterface):
    """Generate Feat specific files

    Examples
    --------

    """

    input_spec = Level1DesignInputSpec
    output_spec = Level1DesignOutputSpec
    
    def _create_ev_file(self, evfname, evinfo):
        f = open(evfname, 'wt')
        for i in evinfo:
            if len(i) == 3:
                f.write('%f %f %f\n' % (i[0], i[1], i[2]))
            else:
                f.write('%f\n' % i[0])
        f.close()

    def _create_ev_files(self, cwd, runinfo, runidx, usetd, contrasts):
        """Creates EV files from condition and regressor information.

           Parameters:
           -----------

           runinfo : dict
               Generated by `SpecifyModel` and contains information
               about events and other regressors.
           runidx  : int
               Index to run number
           usetd   : int
               Whether or not to use temporal derivatives for
               conditions
           contrasts : list of lists
               Information on contrasts to be evaluated
        """
        conds = {}
        evname = []
        ev_hrf = load_template('feat_ev_hrf.tcl')
        ev_none = load_template('feat_ev_none.tcl')
        ev_ortho = load_template('feat_ev_ortho.tcl')
        contrast_header = load_template('feat_contrast_header.tcl')
        contrast_prolog = load_template('feat_contrast_prolog.tcl')
        contrast_element = load_template('feat_contrast_element.tcl')
        contrastmask_header = load_template('feat_contrastmask_header.tcl')
        contrastmask_footer = load_template('feat_contrastmask_footer.tcl')
        contrastmask_element = load_template('feat_contrastmask_element.tcl')
        ev_txt = ''
        # generate sections for conditions and other nuisance
        # regressors
        num_evs = [0, 0]
        for field in ['cond', 'regress']:
            for i, cond in enumerate(runinfo[field]):
                name = cond['name']
                evname.append(name)
                evfname = os.path.join(cwd, 'ev_%s_%d_%d.txt' % (name, runidx,
                                                                 len(evname)))
                evinfo = []
                num_evs[0] += 1
                num_evs[1] += 1
                if field == 'cond':
                    for j, onset in enumerate(cond['onset']):
                        if len(cond['duration']) > 1:
                            evinfo.insert(j, [onset, cond['duration'][j], 1])
                        else:
                            evinfo.insert(j, [onset, cond['duration'][0], 1])
                    ev_txt += ev_hrf.substitute(ev_num=num_evs[0],
                                                ev_name=name,
                                                temporalderiv=usetd,
                                                cond_file=evfname)
                    if usetd:
                        evname.append(name + 'TD')
                        num_evs[1] += 1
                elif field == 'regress':
                    evinfo = [[j] for j in cond['val']]
                    ev_txt += ev_none.substitute(ev_num=num_evs[0],
                                                 ev_name=name,
                                                 cond_file=evfname)
                ev_txt += "\n"
                conds[name] = evfname
                self._create_ev_file(evfname, evinfo)
        # add orthogonalization
        for i in range(1, num_evs[0] + 1):
            for j in range(0, num_evs[0] + 1):
                ev_txt += ev_ortho.substitute(c0=i, c1=j)
                ev_txt += "\n"
        # add t contrast info
        ev_txt += contrast_header.substitute()
        for ctype in ['real', 'orig']:
            for j, con in enumerate(contrasts):
                ev_txt += contrast_prolog.substitute(cnum=j + 1,
                                                     ctype=ctype,
                                                     cname=con[0])
                count = 0
                for c in range(1, len(evname) + 1):
                    if evname[c - 1].endswith('TD') and ctype == 'orig':
                        continue
                    count = count + 1
                    if evname[c - 1] in con[2]:
                        val = con[3][con[2].index(evname[c - 1])]
                    else:
                        val = 0.0
                    ev_txt += contrast_element.substitute(cnum=j + 1,
                                                          element=count,
                                                          ctype=ctype, val=val)
                    ev_txt += "\n"
        # add contrast mask info
        ev_txt += contrastmask_header.substitute()
        for j, _ in enumerate(contrasts):
            for k, _ in enumerate(contrasts):
                if j != k:
                    ev_txt += contrastmask_element.substitute(c1=j + 1,
                                                              c2=k + 1)
        ev_txt += contrastmask_footer.substitute()
        return num_evs, ev_txt
    
    def _get_session_info(self, session_info_file):
        key = 'session_info'
        data = loadflat(session_info_file)
        session_info = data[key]
        if isinstance(session_info, dict):
            session_info = [session_info]
        return session_info

    def _get_func_files(self, session_info):
        """Returns functional files in the order of runs
        """
        func_files = []
        for i, info in enumerate(session_info):
            func_files.insert(i, info['scans'][0].split(',')[0])
        return func_files

    def _run_interface(self, runtime):
        cwd = os.getcwd()
        fsf_header = load_template('feat_header_l1.tcl')
        fsf_postscript = load_template('feat_nongui.tcl')

        prewhiten = 0
        if isdefined(self.inputs.model_serial_correlations):
            prewhiten = int(self.inputs.model_serial_correlations == 'AR(1)')
        usetd = 0
        basis_key = self.inputs.bases.keys()[0]
        if basis_key in ['dgamma', 'gamma']:
            usetd = int(self.inputs.bases[basis_key]['derivs'])
        session_info = self._get_session_info(self.inputs.session_info)
        func_files = self._get_func_files(session_info)

        n_tcon = 0
        n_fcon = 0
        for i, c in enumerate(self.inputs.contrasts):
            if c[1] == 'T':
                n_tcon += 1
            elif c[1] == 'F':
                n_fcon += 1
            else:
                print "unknown contrast type: %s" % str(c)

        if isdefined(self.inputs.register):
            register = int(self.inputs.register)
            reg_image = ''
            reg_dof = 0
            if register:
                reg_image = self.inputs.reg_image
                if not isdefined(reg_image):
                    reg_image = \
                        FSLInfo.standard_image('MNI152_T1_2mm_brain.nii.gz')
                reg_dof = self.inputs.reg_dof
        
        for i, info in enumerate(session_info):
            num_evs, cond_txt = self._create_ev_files(cwd, info, i, usetd,
                                                      self.inputs.contrasts)
            nim = load(func_files[i])
            (_, _, _, timepoints) = nim.get_shape()
            fsf_txt = fsf_header.substitute(run_num=i,
                                            interscan_interval=self.inputs.interscan_interval,
                                            num_vols=timepoints,
                                            prewhiten=prewhiten,
                                            num_evs=num_evs[0],
                                            num_evs_real=num_evs[1],
                                            num_tcon=n_tcon,
                                            num_fcon=n_fcon,
                                            high_pass_filter_cutoff=info['hpf'],
                                            func_file=func_files[i],
                                            register=register,
                                            reg_image=reg_image,
                                            reg_dof=reg_dof)
            fsf_txt += cond_txt
            fsf_txt += fsf_postscript.substitute(overwrite=1)

            f = open(os.path.join(cwd, 'run%d.fsf' % i), 'w')
            f.write(fsf_txt)
            f.close()

        runtime.returncode = 0
        return runtime

    def _list_outputs(self):
        outputs = self.output_spec().get()
        cwd = os.getcwd()
        outputs['fsf_files'] = []
        outputs['ev_files'] = []
        for runno, runinfo in enumerate(self._get_session_info(self.inputs.session_info)):
            outputs['fsf_files'].append(os.path.join(cwd, 'run%d.fsf' % runno))
            evname = []
            for field in ['cond', 'regress']:
                for i, cond in enumerate(runinfo[field]):
                    name = cond['name']
                    evname.append(name)
                    evfname = os.path.join(cwd, 'ev_%s_%d_%d.txt' % (name, runno,
                                                                     len(evname)))
                    outputs['ev_files'].append(evfname)
        return outputs


class FeatInputSpec(FSLTraitedSpec):
    fsf_file = File(exist=True, mandatory=True,argstr="%s", position=0, 
                    desc="File specifying the feat design spec file")
    
class FeatOutputSpec(TraitedSpec):
    featdir = Directory(exists=True)

class Feat(NEW_FSLCommand):
    """Uses FSL feat to calculate first level stats
    """
    _cmd = 'feat'
    input_spec = FeatInputSpec
    output_spec = FeatOutputSpec

    def _list_outputs(self):
        outputs = self._outputs().get()
        outputs['featdir'] = glob(os.path.join(os.getcwd(), '*feat'))[0]
        return outputs

class FeatModelInputSpec(FSLTraitedSpec):
    fsf_file = File(exist=True, mandatory=True, argstr="%s", position=0,
                    desc="File specifying the feat design spec file",
                    copyfile=False)

class FeatModelOutpuSpec(TraitedSpec):
    designfile = File(exists=True, desc='Mat file containing ascii matrix for design')
    confile = File(exists=True, desc='Contrast file containing contrast vectors')

                
# interface to fsl command line model generation routine
# satra: 2010-01-03
class FeatModel(NEW_FSLCommand):
    """Uses FSL feat_model to generate design.mat files
    """
    _cmd = 'feat_model'
    input_spec = FeatModelInputSpec
    output_spec = FeatModelOutpuSpec
    
    def _format_arg(self, name, trait_spec, value):
        if name == 'fsf_file':
            # ohinds: convert fwhm to stddev
            return super(FeatModel, self)._format_arg(name, trait_spec, self._get_design_root(value))
        else:
            return super(FeatModel, self)._format_arg(name, trait_spec, value)

    def _get_design_root(self, infile):
        _, fname = os.path.split(infile)
        return fname.split('.')[0]

    def _list_outputs(self):
        #TODO: figure out file names and get rid off the globs
        outputs = self._outputs().get()
        root = self._get_design_root(list_to_filename(self.inputs.fsf_file))
        designfile = glob(os.path.join(os.getcwd(), '%s*.mat' % root))
        assert len(designfile) == 1, 'No mat file generated by Feat Model'
        outputs['designfile'] = designfile[0]
        confile = glob(os.path.join(os.getcwd(), '%s*.con' % root))
        assert len(confile) == 1, 'No con file generated by Feat Model'
        outputs['confile'] = confile[0]
        return outputs


# interface to fsl command line model fit routines
# ohinds: 2009-12-28
class FilmGLSInputSpec(FSLTraitedSpec):
    infile = File(exists=True, mandatory=True, position=-3,
                  argstr='%s',
                  desc='input data file')
    design_file = File(exists=True, position=-2,
                       argstr='%s',
                       desc='design matrix file')
    threshold = traits.Float(1000, min=0, argstr='%f',
                             position=-1,
                             desc='threshold')      
    smooth_autocorr = traits.Bool(argstr='-sa',
                                  desc='Smooth auto corr estimates')
    mask_size = traits.Int(argstr='-ms %d',
                           desc="susan mask size")
    brightness_threshold = traits.Int(min=0, argstr='-epith %d',
        desc='susan brightness threshold, otherwise it is estimated')
    full_data = traits.Bool(argstr='-v', desc='output full data')
    # Are these mutually exclusive? [SG]
    _estimate_xor = ['autocorr_estimate', 'fit_armodel', 'tukey_window',
                     'multitaper_product', 'use_pava', 'autocorr_noestimate']
    autocorr_estimate = traits.Bool(argstr='-ac',
                                    xor=['autocorr_noestimate'],
                   desc='perform autocorrelation estimatation only')
    fit_armodel = traits.Bool(argstr='-ar',
        desc='fits autoregressive model - default is to use tukey with M=sqrt(numvols)')                      
    tukey_window = traits.Int(argstr='-tukey %d',
        desc='tukey window size to estimate autocorr')
    multitaper_product = traits.Int(argstr='-mt %d',
               desc='multitapering with slepian tapers and num is the time-bandwidth product')
    use_pava = traits.Bool(argstr='-pava', desc='estimates autocorr using PAVA')
    autocorr_noestimate = traits.Bool(argstr='-noest',
                                      xor=['autocorr_estimate'],
                   desc='do not estimate autocorrs')
    output_pwdata = traits.Bool(argstr='-output_pwdata',
                   desc='output prewhitened data and average design matrix')
    results_dir = Directory('results', argstr='-rn %s', usedefault=True,
                            desc='directory to store results in')

class FilmGLSOutputSpec(TraitedSpec):
    param_estimates = OutputMultiPath(File(exists=True),
          desc='Parameter estimates for each column of the design matrix')
    residual4d = File(exists=True,
          desc='Model fit residual mean-squared error for each time point')
    dof_file = File(exists=True, desc='degrees of freedom')
    sigmasquareds = File(exists=True, desc='summary of residuals, See Woolrich, et. al., 2001')
    results_dir = Directory(exists=True,
                         desc='directory storing model estimation output')

class FilmGLS(NEW_FSLCommand):
    """Use FSL film_gls command to fit a design matrix to voxel timeseries

    Examples
    --------
    Initialize with no options, assigning them when calling run:

    >>> from nipype.interfaces import fsl
    >>> fgls = fsl.FilmGLS()
    >>> res = fgls.run('infile', 'designfile', 'thresh', rn='stats')

    Assign options through the ``inputs`` attribute:

    >>> fgls = fsl.FilmGLS()
    >>> fgls.inputs.infile = 'filtered_func_data'
    >>> fgls.inputs.designfile = 'design.mat'
    >>> fgls.inputs.thresh = 10
    >>> fgls.inputs.rn = 'stats'
    >>> res = fgls.run()

    Specify options when creating an instance:

    >>> fgls = fsl.FilmGLS(infile='filtered_func_data', \
                           designfile='design.mat', \
                           thresh=10, rn='stats')
    >>> res = fgls.run()

    """

    _cmd = 'film_gls'
    input_spec = FilmGLSInputSpec
    output_spec = FilmGLSOutputSpec

    def _get_pe_files(self):
        files = None
        if isdefined(self.inputs.designfile):
            fp = open(self.inputs.designfile, 'rt')
            for line in fp.readlines():
                if line.startswith('/NumWaves'):
                    numpes = int(line.split()[-1])
                    files = []
                    cwd = os.getcwd()
                    for i in range(numpes):
                        files.append(self._gen_fname(os.path.join(cwd,
                                                                  'pe%d.nii'%(i+1))))
                    break
            fp.close()
        return files
        
    def _list_outputs(self):
        outputs = self._outputs().get()
        cwd = os.getcwd()
        outputs['results_dir'] = os.path.join(cwd,
                                              self.inputs.results_dir)
        pe_files = self._get_pe_files()
        if pe_files:
            outputs['parameter_estimates'] = pe_files
        outputs['residual4d'] = self._gen_fname(os.path.join(cwd,'res4d.nii'))
        outputs['dof_file'] = os.path.join(cwd,'dof')
        outputs['sigmasquareds'] = self._gen_fname(os.path.join(cwd,'sigmasquareds.nii'))
        return outputs


# satra: 2010-01-23
''' 
class FixedEffectsModel(Interface):
    """Generate Feat specific files

    See FixedEffectsModel().inputs_help() for more information.

    Examples
    --------

    """

    def __init__(self, *args, **inputs):
        self._populate_inputs()
        self.inputs.update(**inputs)

    @property
    def cmd(self):
        return 'feat_fe_design'

    def get_input_info(self):
        """ Provides information about inputs as a dict
            info = [Bunch(key=string,copy=bool,ext='.nii'),...]
        """
        return []

    def inputs_help(self):
        """
        Parameters
        ----------

        feat_dirs : list of directory names
            Lower level feat dirs
        num_copes : int
            number of copes evaluated in each session
        """
        print self.inputs_help.__doc__

    def _populate_inputs(self):
        """ Initializes the input fields of this interface.
        """
        self.inputs = Bunch(feat_dirs=None,
                            num_copes=None)

    def run(self, **inputs):
        self.inputs.update(inputs)
        fsf_header = load_template('feat_fe_header.tcl')
        fsf_footer = load_template('feat_fe_footer.tcl')
        fsf_copes = load_template('feat_fe_copes.tcl')
        fsf_dirs = load_template('feat_fe_featdirs.tcl')
        fsf_ev_header = load_template('feat_fe_ev_header.tcl')
        fsf_ev_element = load_template('feat_fe_ev_element.tcl')

        num_runs = len(filename_to_list(self.inputs.feat_dirs))
        fsf_txt = fsf_header.substitute(num_runs=num_runs,
                                        num_copes=self.inputs.num_copes)
        for i in range(self.inputs.num_copes):
            fsf_txt += fsf_copes.substitute(copeno=i + 1)
        for i, rundir in enumerate(filename_to_list(self.inputs.feat_dirs)):
            fsf_txt += fsf_dirs.substitute(runno=i + 1,
                                           rundir=os.path.abspath(rundir))
        fsf_txt += fsf_ev_header.substitute()
        for i in range(1, num_runs + 1):
            fsf_txt += fsf_ev_element.substitute(input=i)
        fsf_txt += fsf_footer.substitute(overwrite=1)

        f = open(os.path.join(os.getcwd(), 'fixedeffects.fsf'), 'wt')
        f.write(fsf_txt)
        f.close()

        runtime = Bunch(returncode=0,
                        messages=None,
                        errmessages=None)
        outputs = self.aggregate_outputs()
        return InterfaceResult(deepcopy(self), runtime, outputs=outputs)

    def outputs_help(self):
        """
        """
        print self.outputs.__doc__

    def outputs(self):
        """Returns a :class:`nipype.interfaces.base.Bunch` with outputs

        Parameters
        ----------
        (all default to None and are unset)

            fsf_file:
                FSL feat specification file
        """
        outputs = Bunch(fsf_file=None)
        return outputs

    def aggregate_outputs(self):
        outputs = self.outputs()
        outputs.fsf_file = glob(os.path.abspath(os.path.join(os.getcwd(), 'fixed*.fsf')))[0]
        return outputs
'''

class FeatRegisterInputSpec(TraitedSpec):
    feat_dirs = InputMultiPath(Directory(), exist=True, desc="Lower level feat dirs",
                               mandatory=True)
    reg_image = File(exist=True, desc="image to register to (will be treated as standard)",
                     mandatory=True)
    reg_dof = traits.Int(12, desc="registration degrees of freedom", usedefault=True)
    
class FeatRegisterOutputSpec(TraitedSpec):
    fsf_file = File(exists=True,
                                desc="FSL feat specification file")
    
class FeatRegister(NEW_BaseInterface):
    """Register feat directories to a specific standard

    See FixedEffectsModel().inputs_help() for more information.

    Examples
    --------

    """
    input_spec = FeatRegisterInputSpec
    output_spec = FeatRegisterOutputSpec

    def run(self, **inputs):
        self.inputs.set(**inputs)
        runtime = Bunch(returncode=0,
                        stdout=None,
                        stderr=None)
        
        fsf_header = load_template('featreg_header.tcl')
        fsf_footer = load_template('feat_nongui.tcl')
        fsf_dirs = load_template('feat_fe_featdirs.tcl')

        num_runs = len(self.inputs.feat_dirs)
        fsf_txt = fsf_header.substitute(num_runs=num_runs,
                                        regimage=self.inputs.reg_image,
                                        regdof=self.inputs.reg_dof)
        for i, rundir in enumerate(filename_to_list(self.inputs.feat_dirs)):
            fsf_txt += fsf_dirs.substitute(runno=i + 1,
                                           rundir=os.path.abspath(rundir))
        fsf_txt += fsf_footer.substitute()
        f = open(os.path.join(os.getcwd(), 'register.fsf'), 'wt')
        f.write(fsf_txt)
        f.close()
    
        outputs=self.aggregate_outputs()
        return InterfaceResult(deepcopy(self), runtime, outputs=outputs)

    def _list_outputs(self):
        outputs = self._outputs().get()
        outputs['fsf_file'] = os.path.abspath(os.path.join(os.getcwd(), 'register.fsf'))
        return outputs

class FlameoInputSpec(FSLTraitedSpec):
    copefile = File(exists=True, argstr='--copefile=%s', madatory=True)
    varcopefile = File(exists=True, argstr='--varcopefile=%s')
    dofvarcopefile = File(exists=True, argstr='--dofvarcopefile=%s')
    maskfile = File(exists=True, argstr='--maskfile=%s', madatory=True)
    designfile = File(exists=True, argstr='--designfile=%s', madatory=True)
    tconfile = File(exists=True, argstr='--tcontrastsfile=%s', madatory=True)
    fconfile = File(exists=True, argstr='--fcontrastsfile=%s')
    covsplitfile = File(exists=True, argstr='--covsplitfile=%s', madatory=True)
    runmode = traits.Enum('fe', 'ols', 'flame1', 'flame12', argstr='--runmode=%s', madatory=True)
    njumps = traits.Int(argstr='--njumps=%d')
    burnin = traits.Int(argstr='--burnin=%d')
    sampleevery = traits.Int(argstr='--sampleevery=%d')
    fixmean = traits.Bool(argstr='--fixmean')
    inferoutliers = traits.Bool(argstr='--inferoutliers')
    nopeoutput = traits.Bool(argstr='--nopeoutput')
    sigma_dofs = traits.Int(argstr='--sigma_dofs=%d')
    outlier_iter = traits.Int(argstr='--ioni=%d')
    statsdir = Directory("stats", argstr='--ld=%s', usedefaults=True) # ohinds
    flags = traits.Str(argstr='%s')
    # no support for ven, vef


class FlameoOutputSpec(TraitedSpec):
    pes = OutputMultiPath(exists=True, desc="Parameter estimates for each column of the design matrix" +
                "for each voxel")
    varcopes = OutputMultiPath(exists=True, desc="Variance estimates")
    res4d = OutputMultiPath(exists=True, desc="Model fit residual mean-squared error for each time point")
    copes = OutputMultiPath(exists=True, desc="Contrast estimates for each contrast")
    varcopes = OutputMultiPath(exists=True, desc="Variance estimates for each contrast")
    zstats = OutputMultiPath(exists=True, desc="z-stat file for each contrast")
    tstats = OutputMultiPath(exists=True, desc="t-stat file for each contrast")
    statsdir = Directory(exists=True, desc="directory storing model estimation output")


# interface to fsl command line higher level model fit
# satra: 2010-01-09
class Flameo(NEW_FSLCommand):
    """Use FSL flameo command to perform higher level model fits

    To print out the command line help, use:
        fsl.Flameo().inputs_help()

    Examples
    --------
    Initialize Flameo with no options, assigning them when calling run:

    >>> from nipype.interfaces import fsl
    >>> flame = fsl.Flameo()
    >>> res = flame.run()

    >>> from nipype.interfaces import fsl
    >>> import os
    >>> flameo = fsl.Flameo(copefile='cope.nii.gz', \
                            varcopefile='varcope.nii.gz', \
                            designfile='design.mat', \
                            tconfile='design.con', \
                            runmode='fe')
    >>> flameo.cmdline
    'flameo --copefile=cope.nii.gz --designfile=design.mat --runmode=fe --tcontrastsfile=design.con --varcopefile=varcope.nii.gz'
    """
    _cmd = 'flameo'
    input_spec = FlameoInputSpec
    output_spec = FlameoOutputSpec

    # ohinds: 2010-04-06
    def _run_interface(self, runtime):
        statsdir = self.inputs.statsdir
        cwd = os.getcwd()
        if os.access(os.path.join(cwd, statsdir), os.F_OK):
            rmtree(os.path.join(cwd, statsdir))

        return super(Flameo, self)._run_interface(runtime)

    # ohinds: 2010-04-06
    # made these compatible with flameo
    def _list_outputs(self):
        outputs = self._outputs().get()
        pth = os.path.join(os.getcwd(), self.inputs.statsdir)

        pes = glob(os.path.join(pth, 'pe[0-9]*.*'))
        assert len(pes) >= 1, 'No pe volumes generated by FSL Estimate'
        outputs['pes'] = pes

        res4d = glob(os.path.join(pth, 'res4d.*'))
        assert len(res4d) == 1, 'No residual volume generated by FSL Estimate'
        outputs['res4d'] = res4d[0]

        copes = glob(os.path.join(pth, 'cope[0-9]*.*'))
        assert len(copes) >= 1, 'No cope volumes generated by FSL CEstimate'
        outputs['copes'] = copes

        varcopes = glob(os.path.join(pth, 'varcope[0-9]*.*'))
        assert len(varcopes) >= 1, 'No varcope volumes generated by FSL CEstimate'
        outputs['varcopes'] = varcopes

        zstats = glob(os.path.join(pth, 'zstat[0-9]*.*'))
        assert len(zstats) >= 1, 'No zstat volumes generated by FSL CEstimate'
        outputs['zstats'] = zstats

        tstats = glob(os.path.join(pth, 'tstat[0-9]*.*'))
        assert len(tstats) >= 1, 'No tstat volumes generated by FSL CEstimate'
        outputs['tstats'] = tstats

        mrefs = glob(os.path.join(pth, 'mean_random_effects_var[0-9]*.*'))
        assert len(mrefs) >= 1, 'No mean random effects volumes generated by Flameo'
        outputs['mrefs'] = mrefs

        tdof = glob(os.path.join(pth, 'tdof_t[0-9]*.*'))
        assert len(tdof) >= 1, 'No T dof volumes generated by Flameo'
        outputs['tdof'] = tdof

        weights = glob(os.path.join(pth, 'weights[0-9]*.*'))
        assert len(weights) >= 1, 'No weight volumes generated by Flameo'
        outputs['weights'] = weights

        outputs['statsdir'] = pth

        return outputs

class ContrastMgrInputSpec(FSLTraitedSpec):
    tcon_file = File(exists=True, mandatory=True,
                     argstr='%s', position=-1,
                     desc='contrast file containing T-contrasts')
    fcon_file = File(exists=True, argstr='-f %s',
                     desc='contrast file containing T-contrasts')
    stats_dir = Directory(exists=True, mandatory=True,
                          argstr='%s', position=-2,
                          copyfile=False,
                          desc='directory containing first level analysis')
    contrast_num = traits.Int(min=1, argstr='-cope',
                desc='contrast number to start labeling copes from')
    suffix = traits.Str(argstr='-suffix %s',
                        desc='suffix to put on the end of the cope filename before the contrast number, default is nothing')

class ContrastMgrOutputSpec(TraitedSpec):
    cope_files = OutputMultiPath(File(exists=True),
                                 desc='Contrast estimates for each contrast')
    varcope_files = OutputMultiPath(File(exists=True),
                                 desc='Variance estimates for each contrast')
    zstat_files = OutputMultiPath(File(exists=True),
                                 desc='z-stat file for each contrast')
    tstat_files = OutputMultiPath(File(exists=True),
                                 desc='t-stat file for each contrast') 
    fstat_files = OutputMultiPath(File(exists=True),
                                 desc='f-stat file for each contrast') 
    neff_files =  OutputMultiPath(File(exists=True),
                                 desc='neff file ?? for each contrast')

class ContrastMgr(NEW_FSLCommand):
    """Use FSL contrast_mgr command to evaluate contrasts

    Examples
    --------
    """

    _cmd = 'contrast_mgr'
    input_spec = ContrastMgrInputSpec
    output_spec = ContrastMgrOutputSpec

    def _get_files(self):
        files = None
        if isdefined(self.inputs.tcon_file):
            fp = open(self.inputs.tcon_file, 'rt')
            for line in fp.readlines():
                if line.startswith('/NumWaves'):
                    numpes = int(line.split()[-1])
                    files = []
                    cwd = os.getcwd()
                    for i in range(numpes):
                        files.append(self._gen_fname(os.path.join(cwd,
                                                                  'pe%d.nii'%(i+1))))
                    break
            fp.close()
        return files
    
    def _list_outputs(self):
        outputs = self._outputs().get()
        pth = self.inputs.stats_dir
        
        #TODO: figure out file names and get rid off the globs
        # use something like _get_files above

        copes = glob(os.path.join(pth, 'cope[0-9]*.*'))
        assert len(copes) >= 1, 'No cope volumes generated by FSL CEstimate'
        outputs['cope_files'] = copes

        varcopes = glob(os.path.join(pth, 'varcope[0-9]*.*'))
        assert len(varcopes) >= 1, 'No varcope volumes generated by FSL CEstimate'
        outputs['varcope_files'] = varcopes

        zstats = glob(os.path.join(pth, 'zstat[0-9]*.*'))
        assert len(zstats) >= 1, 'No zstat volumes generated by FSL CEstimate'
        outputs['zstats_files'] = zstats

        tstats = glob(os.path.join(pth, 'tstat[0-9]*.*'))
        assert len(tstats) >= 1, 'No tstat volumes generated by FSL CEstimate'
        outputs['tstats_files'] = tstats

        fstats = glob(os.path.join(pth, 'fstat[0-9]*.*'))
        if fstats:
            outputs['fstats_files'] = fstats

        neffs = glob(os.path.join(pth, 'neff[0-9]*.*'))
        assert len(neffs) >= 1, 'No neff volumes generated by FSL CEstimate'
        outputs['neff_files'] = neffs
        
        return outputs

class L2ModelInputSpec(TraitedSpec):
    num_copes = traits.Int(min=1, mandatory=True,
                             desc='number of copes to be combined')

class L2ModelOutputSpec(TraitedSpec):
    design_mat = File(exists=True, desc='design matrix file')
    design_con = File(exists=True, desc='design contrast file')
    design_grp = File(exists=True, desc='design group file')

class L2Model(NEW_BaseInterface):
    """Generate subject specific second level model

    Examples
    --------

    >>> from nipype.interfaces.fsl import L2Model
    >>> model = L2Model(num_copes=3) # 3 sessions

    """

    input_spec = L2ModelInputSpec
    output_spec = L2ModelOutputSpec

    def _run_interface(self, runtime):
        cwd = os.getcwd()
        mat_txt = ['/NumWaves       1',
                   '/NumPoints      %d' % self.inputs.num_copes,
                   '/PPheights      %e' % 1,
                   '',
                   '/Matrix']
        for i in range(self.inputs.num_copes):
            mat_txt += ['%e' % 1]
        mat_txt = '\n'.join(mat_txt)
        
        con_txt = ['/ContrastName1   group mean',
                   '/NumWaves       1',
                   '/NumContrasts   1',
                   '/PPheights          %e' % 1,
                   '/RequiredEffect     100.0', #XX where does this
                   #number come from
                   '',
                   '/Matrix',
                   '%e' % 1]
        con_txt = '\n'.join(con_txt)

        grp_txt = ['/NumWaves       1',
                   '/NumPoints      %d' % self.inputs.num_copes,
                   '',
                   '/Matrix']
        for i in range(self.inputs.num_copes):
            grp_txt += ['1']
        grp_txt = '\n'.join(grp_txt)
        
        txt = {'design.mat' : mat_txt,
               'design.con' : con_txt,
               'design.grp' : grp_txt}

        # write design files
        for i, name in enumerate(['design.mat','design.con','design.grp']):
            f = open(os.path.join(cwd, name), 'wt')
            f.write(txt[name])
            f.close()

        runtime.returncode=0
        return runtime

    def _list_outputs(self):
        outputs = self._outputs().get()
        for field in outputs.keys():
            setattr(outputs, field, os.path.join(os.getcwd(),
                                                 field.replace('_','.')))
        return outputs

class SMMInputSpec(FSLTraitedSpec):
    spatialdatafile = File(exists=True, position=0, argstr='--sdf="%s"', mandatory=True,
                           desc="statistics spatial map", copyfile=False)
    mask = File(exist=True, position=1, argstr='--mask="%s"', mandatory=True,
                desc="mask file", copyfile=False)
    zfstatmode = traits.Bool(position=2, argstr="--zfstatmode",
                             desc="enforces no deactivation class")

class SMMOutputSpec(TraitedSpec):
    null_p_map = File(exists=True)
    activation_p_map = File(exists=True)
    deactivation_p_map = File(exists=True)

class SMM(NEW_FSLCommand):
    '''
    Spatial Mixture Modelling. For more detail on the spatial mixture modelling see 
    Mixture Models with Adaptive Spatial Regularisation for Segmentation with an Application to FMRI Data; 
    Woolrich, M., Behrens, T., Beckmann, C., and Smith, S.; IEEE Trans. Medical Imaging, 24(1):1-11, 2005. 
    '''
    _cmd = 'mm --ld=logdir'
    input_spec = SMMInputSpec
    output_spec = SMMOutputSpec

    def _list_outputs(self):
        outputs = self._outputs().get()
        #TODO get the true logdir from the stdout
        outputs['null_p_map'] = self._gen_fname(basename="w1_mean", cwd="logdir")
        outputs['activation_p_map'] = self._gen_fname(basename="w2_mean", cwd="logdir")
        if not isdefined(self.inputs.zfstatmode) or not self.inputs.zfstatmode:
            outputs['deactivation_p_map'] = self._gen_fname(basename="w3_mean", cwd="logdir")
        return outputs
