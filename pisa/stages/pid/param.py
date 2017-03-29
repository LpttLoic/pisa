#
# PISA authors: Lukas Schulte
#               schulte@physik.uni-bonn.de
#               Justin L. Lanfranchi
#               jll1062+pisa@phys.psu.edu
#
# CAKE author: Shivesh Mandalia
#              s.p.mandalia@qmul.ac.uk
#
# date:    2016-05-13
"""
The purpose of this stage is to simulate event classification like that used
for PINGU, sorting the reconstructed nue CC, numu CC, nutau CC, and NC events
into the track and cascade channels.

This service in particular takes in events from a PISA HDF5 file to transform
a set of input maps into a set of track and cascade maps.

For each particle "signature," a histogram in the input binning dimensions is
created, which gives the PID probabilities in each bin. The input maps are
transformed according to these probabilities to provide an output containing a
map for track-like events ('trck') and shower-like events ('cscd'), which is
then returned.

"""


from collections import OrderedDict
from copy import deepcopy
from itertools import product

import numpy as np
from scipy.stats import norm

from pisa.core.binning import OneDimBinning
from pisa.core.stage import Stage
from pisa.core.transform import BinnedTensorTransform, TransformSet
from pisa.utils.fileio import from_file
from pisa.utils.flavInt import flavintGroupsFromString, NuFlavIntGroup
from pisa.utils.hash import hash_obj
from pisa.utils.log import logging
from pisa.utils.profiler import profile
from pisa.core.map import Map, MapSet


__all__ = ['hist']


class param(Stage):
    """Parameterised MC PID based on an input json file containing functions 
    describing the PID as a function of energy.

    Transforms an input map of the specified particle "signature" (aka ID) into
    a map of the track-like events ('trck') and a map of the shower-like events
    ('cscd').

    Parameters
    ----------
    params : ParamSet or sequence with which to instantiate a ParamSet

        Parameters which set everything besides the binning.

        If str, interpret as resource location and load params from resource.
        If dict, set contained params. Format expected is
            {'<param_name>': <Param object or passable to Param()>}

        Parameters required by this service are
            * pid_energy_paramfile : dict or filepath
                json file or equivalent dict containing the PID functions for 
                each flavour. The structure should be:
                  {
                    "numu_cc": {
                      "trck" : "lambda E: some function",
                      "cscd" : "lambda E: 1 - some function"
                    },
                    "nue_cc": {
                      "trck" : "lambda E: some function",
                      "cscd" : "lambda E: 1 - some function"
                    },
                    "nutau_cc": {
                      "trck" : "lambda E: some function",
                      "cscd" : "lambda E: 1 - some function"
                    },
                    "nuall_nc": {
                      "trck" : "lambda E: some function",
                      "cscd" : "lambda E: 1 - some function"
                    }
                  }

    particles

    input_names

    transform_groups

    TODO: sum_grouped_flavints

    input_binning : MultiDimBinning
        Arbitrary number of dimensions accepted. Contents of the input
        `pid_events` parameter defines the possible binning dimensions. Name(s)
        of given binning(s) must match to a reco variable in `pid_events`.

    output_binning : MultiDimBinning

    error_method : None, bool, or string

    transforms_cache_depth : int >= 0

    outputs_cache_depth : int >= 0

    memcache_deepcopy : bool

    debug_mode : None, bool, or string
        Whether to store extra debug info for this service.


    Input Names
    ----------
    The `inputs` container must include objects with `name` attributes:
        * 'nue_cc'
        * 'nuebar_cc'
        * 'numu_cc'
        * 'numubar_cc'
        * 'nutau_cc'
        * 'nutaubar_cc'
        * 'nuall_nc'
        * 'nuallbar_nc'

    Output Names
    ----------
    The `outputs` container generated by this service will be objects with the
    following `name` attribute:
        * 'nue_cc_trck'
        * 'nue_cc_cscd'
        * 'nuebar_cc_trck'
        * 'nuebar_cc_cscd'
        * 'numu_cc_trck'
        * 'numu_cc_cscd'
        * 'numubar_cc_trck'
        * 'numubar_cc_cscd'
        * 'nutau_cc_trck'
        * 'nutau_cc_cscd'
        * 'nutaubar_cc_trck'
        * 'nutaubar_cc_cscd'
        * 'nuall_nc_trck'
        * 'nuall_nc_cscd'
        * 'nuallbar_nc_trck'
        * 'nuallbar_nc_cscd'

    """
    # TODO: add sum_grouped_flavints instantiation arg
    def __init__(self, params, particles, input_names, transform_groups,
                 input_binning, output_binning, memcache_deepcopy,
                 error_method, transforms_cache_depth,
                 outputs_cache_depth, debug_mode=None):
        assert particles in ['muons', 'neutrinos']
        self.particles = particles
        """Whether stage is instantiated to process neutrinos or muons"""

        self.transform_groups = flavintGroupsFromString(transform_groups)
        """Particle/interaction types to group for computing transforms"""

        # TODO
        #self.sum_grouped_flavints = sum_grouped_flavints

        # All of the following params (and no more) must be passed via
        # the `params` argument.
        expected_params = (
            'pid_energy_paramfile'
        )

        if isinstance(input_names, basestring):
            input_names = input_names.replace(' ', '').split(',')

        # Define the names of objects that get produced by this stage
        self.output_channels = ('trck', 'cscd')
        #output_names = [self.suffix_channel(in_name, out_chan) for in_name,
        #                out_chan in product(input_names, self.output_channels)]

        super(self.__class__, self).__init__(
            use_transforms=True,
            params=params,
            expected_params=expected_params,
            input_names=input_names,
            output_names=input_names,
            error_method=error_method,
            outputs_cache_depth=outputs_cache_depth,
            transforms_cache_depth=transforms_cache_depth,
            memcache_deepcopy=memcache_deepcopy,
            input_binning=input_binning,
            output_binning=output_binning,
            debug_mode=debug_mode
        )

        self.include_attrs_for_hashes('particles')
        self.include_attrs_for_hashes('transform_groups')

    def validate_binning(self):
        # Must have energy in input binning
        if 'reco_energy' not in set(self.input_binning.names):
            raise ValueError(
                'Input binning must contain "reco_energy".'
            )

        # Right now this can only deal with 1D energy or 2D energy / coszenith
        # binning, so if azimuth is present then this will raise an exception.
        if 'reco_azimuth' in set(self.input_binning.names):
            raise ValueError(
                "Input binning cannot have azimuth present for this "
                "parameterised PID service."
            )

        if (self.input_binning.names[0] != 'reco_energy' and
                self.input_binning.names[0] != 'reco_coszen'):
            raise ValueError(
                "Got a name for the first binning dimension that"
                " was unexpected - '%s'."%self.input_binning.names[0]
            )

        # TODO: not handling rebinning in this stage or within Transform
        # objects; implement this! (and then this assert statement can go away)
        #assert self.input_binning == self.output_binning, \
        #        "input and output binning deviate!"

    def load_pid_energy_param(self, pid_energy_param):
        """
        Load pid energy-dependent parameterisation from file or dictionary.
        """
        this_hash = hash_obj(pid_energy_param)
        if (hasattr(self, '_energy_param_hash') and
            this_hash == self._energy_param_hash):
            return
        if isinstance(pid_energy_param, basestring):
            energy_param_dict = from_file(pid_energy_param)
        elif isinstance(pid_energy_param, dict):
            energy_param_dict = pid_energy_param
        self.energy_param_dict = energy_param_dict
        self._energy_param_hash = this_hash

    def find_energy_param(self, flavstr):
        """
        Load the specific energy parameterisation from the dictionary.
        """
        if flavstr not in self.energy_param_dict.keys():
            if '+' in flavstr:
                nubar = flavstr.split('+')[-1]
                nu = flavstr.split('+')[0]
                if nubar.replace('bar','') == nu:
                    if nu in self.energy_param_dict.keys():
                        energy_param = self.energy_param_dict[nu]
                    else:
                        raise ValueError(
                            "Got flavour '%s' which is not in the "
                            "parameterisation dictionary keys - %s"
                            %(nu, self.energy_param_dict.keys())
                        )
                else:
                    raise ValueError(
                        "Expected to get joined flav of nu+nubar but instead "
                        "got '%s'."%flavstr
                    )
            else:
                raise ValueError(
                    "Got flavour '%s' which is not in the parameterisation "
                    " dictionary keys - %s or a flavour combination."
                    %(flavstr,self.energy_param_dict.keys())
                )
        else:
            energy_param = self.energy_param_dict[flavstr]
        return energy_param

    @profile
    def _compute_nominal_transforms(self):
        """Compute new PID transforms."""
        logging.debug('Updating pid.param PID histograms...')

        ecen = self.input_binning.reco_energy.weighted_centers.magnitude

        self.load_pid_energy_param(self.params.pid_energy_paramfile.value)

        # Derive transforms by combining flavints that behave similarly, but
        # apply the derived transforms to the input flavints separately
        # (leaving combining these together to later)
        nominal_transforms = []
        for flav_int_group in self.transform_groups:
            logging.debug("Working on %s PID" %flav_int_group)
            # Get the parameterisation
            energy_param = self.find_energy_param(str(flav_int_group))
            # Should be a dict
            if not isinstance(energy_param, dict):
                raise TypeError(
                    "Loaded energy PID parameterisation should be a dictionary"
                    " but got '%s.'"%type(energy_param)
                )
            # ...with the same keys as the output channels
            if list(self.output_channels) != energy_param.keys():
                raise ValueError(
                    "Expected output channels, %s, does not match the list of"
                    " PID classifications in the energy PID parameterisation "
                    "- %s."%(
                        list(self.output_channels),
                        energy_param.keys()
                    )
                )
            for sig in self.output_channels:
                pid_param = energy_param[sig]    
                if not isinstance(pid_param, basestring):
                    raise TypeError(
                        "Got '%s' for the parameterisation while expected a "
                        "string."%type(pid_param)
                    )
                else:
                    if 'scipy.stats.norm' in pid_param:
                        pid_param = pid_param.replace(
                            'scipy.stats.norm', 'norm'
                        )
                    pid_param = eval(pid_param)
                # Get the PID probabilities for the energy bins in the analysis
                pid1d = pid_param(ecen)
                # Make this in to the right dimensionality.
                if 'reco_coszen' in set(self.input_binning.names):
                    czcen = self.input_binning[
                        'reco_coszen'
                    ].weighted_centers.magnitude
                    pid2d = np.reshape(np.repeat(pid1d, len(czcen)),
                                       (len(ecen), len(czcen)))
                    if self.input_binning.names[0] == 'reco_coszen':
                        pid2d = pid2d.T
                    xform_array = pid2d
                else:
                    xform_array = pid1d

                # Copy this transform to use for each input in the group
                for input_name in self.input_names:
                    if input_name not in flav_int_group:
                        continue
                    xform = BinnedTensorTransform(
                        input_names=input_name,
                        output_name=self.suffix_channel(input_name, sig),
                        input_binning=self.input_binning,
                        output_binning=self.input_binning,
                        xform_array=xform_array
                    )
                    nominal_transforms.append(xform)

        return TransformSet(transforms=nominal_transforms)

    def get_outputs(self, inputs=None):
        orig_output_binning = self.output_binning
        self.output_binning = self.input_binning
        outputs = super(self.__class__, self).get_outputs(inputs)
        # If PID is not in the original output binning, add in a dummy PID
        # binning so that the assertion on the output binning does not fail.
        if 'pid' not in orig_output_binning.names:
            dummy_pid = OneDimBinning(name='pid', bin_edges=[0,1,2])
            self.output_binning = orig_output_binning + dummy_pid
        else:
            self.output_binning = orig_output_binning
        # put together pid bins
        new_maps = []
        for name in self.output_names:
            hist = np.array([outputs[name+'_cscd'].hist,
                             outputs[name+'_trck'].hist])
            # put that pid dimension last
            hist = np.rollaxis(hist, 0, 3)
            new_maps.append(Map(name, hist, self.output_binning))
        new_outputs = MapSet(new_maps, outputs.name, outputs.tex)
        return new_outputs

    def check_transforms(self, transforms):
        pass

    def check_outputs(self, outputs):
        pass

    def _compute_transforms(self):
        """There are no systematics in this stage, so the transforms are just
        the nominal transforms. Thus, this function just returns the nominal
        transforms, computed by `_compute_nominal_transforms`..

        """
        return self.nominal_transforms

    def suffix_channel(self, flavint, channel):
        return '%s_%s' % (flavint, channel)

    def validate_params(self, params):
        # do some checks on the parameters
        f = params.pid_energy_paramfile.value
        # Check type of pid_energy_paramfile
        if not isinstance(f, (basestring, dict)):
            raise TypeError(
                "Expecting either a path to a file or a dictionary provided "
                "as the store of the parameterisations. Got '%s'."%type(f)
            )