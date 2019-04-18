#!/usr/bin/env python2.7

from __future__ import print_function

from scipy.stats import lognorm
from collections import OrderedDict
import dendropy
import collections
import pandas as pd
import numpy as np
import itertools
import random
import sys
import os
import MESS
from MESS.util import tuplecheck, sample_param_range, MESSError, set_params

import logging
LOGGER = logging.getLogger(__name__)

## Limit on the number of redraws in the event of disallowed
## multiple migration, error out and warn if exceeded
MAX_DUPLICATE_REDRAWS_FROM_METACOMMUNITY = 1500

## id is a variable length string so we set the dtype as "object"
## to allow for reference pointing to string objects
METACOMMUNITY_DTYPE = np.dtype([('ids', object),
                                ('immigration_probabilities', 'f8'),
                                ('abundances', 'i8'),
                                ('trait_values', 'f8')])

class Metacommunity(object):

    def __init__(self, meta_type="logser", quiet=False):
        self.quiet = quiet

        ## If you add a parameter to this dictionary you need
        ## to also add a short description to the LOCAL_PARAMS dict
        ## at the end of this file
        ##
        ## Also be sure to add it to _paramschecker so the type gets set correctly
        self.paramsdict = OrderedDict([
                        ("S_m", 100),
                        ("J_m", 750000),
                        ("speciation_rate", 2),
                        ("death_proportion", 0.7),
                        ("trait_rate_meta", 2),
                        ("ecological_strength", 5),
        ])

        ## elite hackers only internal dictionary, normally you shouldn't mess with this
        ##  * metacommunity_type: Options: uniform/logser/<filename>
        ##  * lognorm_shape: Shape parameter of the lognormal distribution, if you
        ##      choose that option, otherwise it does nothing.
        ##  * filtering_optimum: optimum trait value, only used during environmental
        ##      filtering model. It isn't even a real parameter at this point because
        ##      you can't set it, it's constructed during the simulation.
        self._hackersonly= OrderedDict([
                        ("metacommunity_type", meta_type),
                        ("lognorm_shape", 1.98),
                        ("filtering_optimum", 1)
        ])

        ## A dictionary for holding prior ranges for values we're interested in
        self._priors = dict([
                        ("S_m", []),
                        ("J_m", []),
                        ("speciation_rate", []),
                        ("death_proportion", []),
                        ("trait_rate_meta", []),
                        ("ecological_strength", []),
        ])

        ## The Newick formatted tree for the metacommunity
        self.metacommunity_tree = ""

        ## A structured numpy array for holding tip labels, abundances, colonization
        ## probabilities and trait values
        self.community = []

        self.set_metacommunity()
        LOGGER.debug("Metacommunity paramsdict - {}".format(self.paramsdict))


    def __str__(self):
        return "<Metacommunity: {} Richness {}>".format(self._hackersonly["metacommunity_type"],\
                                                        self.paramsdict["S_m"])

    def _resample_priors(self):
        for k,v in self._priors.items():
            if np.array(v).any():
                self.paramsdict[k] = sample_param_range(v)[0]


    def _simulate_metacommunity(self, J, S_m, speciation_rate, death_proportion, trait_rate_meta):
        import rpy2.robjects as robjects
        from rpy2.robjects import r, pandas2ri

        make_meta = """## function to make the mainland meta community with a phylo, traits, and abundances
        ## required packages:
        ##    ape,
        ##    TreeSim,
        ##    pika
        ## arguments:
        #' @param J the number of individuals in the meta community
        #' @param S_m the number of species in the meta community
        #' @param speciation_rate the speciation rate
        #' @param death_proportion the proportional extinction rate
        #' @param trait_rate_meta the rate of brownian motion

        makeMeta <- function(Jm, S, lambda, deathFrac, sigma2) {
          ## the tree
          tre <- TreeSim::sim.bd.taxa(S, numbsim = 1, lambda = lambda, mu = lambda * deathFrac,
                                      complete = FALSE)[[1]]

          ## the traits
          trt <- ape::rTraitCont(tre, sigma = sqrt(sigma2))
          trt <- data.frame(name = names(trt), value = as.numeric(trt))

          ## parameters for the log-series
          nBar <- Jm / S
          p <- 1 - 1/nBar
          b <- -log(p)

          ## the abundances
          #abund <- sads::rls(length(trt), length(trt), 0.01)
          abund <- meteR::sad(meteR::meteESF(S0 = S, N0 = Jm))$r(S)

          ## return it all in a list
          tre <- ape::write.tree(tre)
          return(list(phylo = tre, traits = trt, abundance = abund))
        }"""

        make_meta_func = robjects.r(make_meta)
        res = pandas2ri.ri2py(make_meta_func(J, S_m, speciation_rate, death_proportion, trait_rate_meta))
        tree = res[0][0]
        traits = pandas2ri.ri2py(res[1])
        abunds = pandas2ri.ri2py(res[2])
        return tree, abunds, traits


    def _paramschecker(self, param, newvalue, quiet=True):
        """ Raises exceptions when params are set to values they should not be"""
        ## TODO: This should actually check the values and make sure they make sense
        try:
            if (not quiet) and MESS.__interactive__:
                print("  Updating Metacommunity parameters requires running set_metacommunity()"\
                        + " to apply the changes.")

            ## Cast params to correct types
            if param in ["S_m", "J_m", "speciation_rate", "death_proportion", "trait_rate_meta",
                            "ecological_strength"]:
                tup = tuplecheck(newvalue, dtype=float)
                if isinstance(tup, tuple):
                    self._priors[param] = tup
                    self.paramsdict[param] = sample_param_range(tup)[0]
                else:
                    self.paramsdict[param] = tup
                LOGGER.debug("{} {}".format(param, tup))

        except Exception as inst:
            ## Do something intelligent here?
            raise MESSError("Error {}\n    Bad parameter {} - Bad value {}".format(inst, param, newvalue))


    def _get_params_header(self):
        return list(self.paramsdict.keys())


    def _get_params_values(self):
        return list(self.paramsdict.values())


    def _get_trait_values(self):
        return list(self.community["trait_values"])


    def write_params(self, outfile=None, full=False, append=True):
        """
        Write out the parameters for this island to a file.
        Normally this isn't called directly, but by the main
        simulation engine.

        append
        """
        if outfile is None:
            raise MESSError("Metacommunity.write_params outfile must be specified.")

        ## If not appending then we are overwriting
        if append:
            filemode = 'a'
        else:
            filemode = 'w'

        with open(outfile, filemode) as paramsfile:
            ## Only write the full header if not appending
            if not append:
                header = "------- MESS params file (v.{})".format(MESS.__version__)
                header += ("-"*(80-len(header)))
                paramsfile.write(header)

            header = "------- Metacommunity params: "
            header += ("-"*(80-len(header)))
            paramsfile.write(header)

            for key, val in self.paramsdict.items():
                paramvalue = str(val)

                ## If it's one of the params with a prior, and if the prior is not
                ## empty and if writing out full, then write the prior, and not
                ## the sampled value
                if full:
                    if key in list(self._priors.keys()):
                        if self._priors[key]:
                            paramvalue = "-".join([str(i) for i in self._priors[key]])

                padding = (" "*(20-len(paramvalue)))
                paramkey = list(self.paramsdict.keys()).index(key)
                paramindex = " ## [{}] ".format(paramkey)
                LOGGER.debug("{} {} {}".format(key, val, paramindex))
                #name = "[{}]: ".format(paramname(paramkey))
                name = "[{}]: ".format(key)
                #description = paraminfo(paramkey, short=True)
                description = LOCAL_PARAMS[key]
                paramsfile.write("\n" + paramvalue + padding + \
                                        paramindex + name + description)

            paramsfile.write("\n")


    def set_metacommunity(self, random=False, resample=False):
        """
        For setting the metacommunity you can either generate a random
        uniform community or read on in from a file that's basically just
        a long list of abundances (as ints). Abundances are set from one
        of these locations then the species labels and immigration probs
        are calculated from there

        random=True will set random trait values in the range [0-1]
        """
        meta_type = self._hackersonly["metacommunity_type"]
        LOGGER.debug("Enter set_metacommunity - {}".format(meta_type))

        if resample:
            self._resample_priors()

        ## Accumulators for bringing in all the values. These will
        ## eventually all get shoved into self.community
        abundances = np.array([])
        ids = np.array([])
        trait_values = np.array([])

        ## Two distributions are being left in here as hidden options, the ids and trait values
        ## will get populated below
        ##
        ## These are for testing purposes and should normally be ignored
        if meta_type == "lognorm":
            abundances = lognorm.rvs(self._hackersonly["lognorm_shape"],\
                                        loc=1,
                                        size=self.paramsdict["S_m"])

        elif meta_type == "uniform":
            abundances = np.array([self.paramsdict["J_m"] / self.paramsdict["S_m"]]\
                               * self.paramsdict["S_m"])

        ## Get Abundances by simulating a tree, evolving traits on it, and sprinkling abundances
        ## This is the primary means of setting the metacommunity that is driven by Andy's
        ## R code.
        elif meta_type == "logser":
            tree, abunds, traits = self._simulate_metacommunity(self.paramsdict["J_m"],\
                                                                self.paramsdict["S_m"],\
                                                                self.paramsdict["speciation_rate"],\
                                                                self.paramsdict["death_proportion"],\
                                                                self.paramsdict["trait_rate_meta"])
            #handle = Phylo.read(StringIO(tree), "newick")
            handle = dendropy.Tree.get(data=tree, schema="newick")
            self.metacommunity_tree = handle

            abundances = abunds
            ## TODO: This is dumb
            tups = list(zip(traits["name"], traits["value"]))
            ids = np.array([x[0] for x in tups])
            trait_values = np.array([x[1] for x in tups])

            self._hackersonly["filtering_optimum"] = np.random.normal(loc=np.mean(trait_values), scale=np.std(trait_values), size=1)[0]

        ## Attempt to read tree/ids/abunds/traits from a file. If it fails, fall back to just
        ## try reading the old list of abundances format.
        ##
        ## TODO: This input file format doesn't include a tree, should we just delete this option?
        ## Both of these input file formats should be considered deprecated for the most part.
        elif os.path.isfile(meta_type):
            try:
                with open(meta_type, 'r') as infile:
                    lines = infile.readlines()
                    self.metcommunity_tree_height = float(lines[0].split()[0])
                    self.paramsdict["trait_rate_meta"] = float(lines[1].split()[0])

                    for i in range(2,len(lines)):
                        info = lines[i].split()
                        ## Doing these somewhat out of order because i'm relying
                        ## on the info[1] failing and raising for the old type
                        ## file that is only a list of abundances. If you do the
                        ## ids[0] first it'll succeed and then fuck up the ids col
                        ## downstream.
                        np.append(trait_values, float(info[1]))
                        np.append(abundances, int(info[2]))
                        np.append(ids, info[0])
            except IndexError as inst:
                ## Could be an old style file just containing abundances, one per row
                try:
                    with open(meta_type, 'r') as infile:
                        lines = infile.readlines()
                        abundances = [int(line.split()[0]) for line in lines]
                except Exception as inst:
                    raise MESSError("  Malformed metacommunity specification file - {}\n    {}".format(meta_type, inst))

            ## If reading from a file then the number of species will not correspond
            ## with the value already in the paramsdict, so we need to update the S_m count
            ## and reup the community ndarray
            LOGGER.debug("Read S_m from file - {}".format(len(abundances)))
            self.paramsdict["S_m"] = len(abundances)
        else:
            raise MESSError("  Unrecognized metacommunity input - {}".format(meta_type))

        ## This next set of conditionals is responsible for filling in trait values and ids
        ## for the abundance only file and the 2 distributions specified at the beginning.
        ## TODO: optionally set random trait values?

        if random or not trait_values.size:
            LOGGER.debug("Using random trait values")
            trait_values = np.random.rand(self.paramsdict["S_m"])

        ## If ids haven't been assigned yet, do that here
        if not ids.size:
            ids = np.array(["t"+str(x) for x in range(0, self.paramsdict["S_m"])])

        ## TODO: You can use msprime to simulate a random coalescent tree and get a newick from it.
        ## Is this worth doing?
        if not self.metacommunity_tree:
            pass

        self.community = np.zeros([self.paramsdict["S_m"]], dtype=METACOMMUNITY_DTYPE)
        ## Populate the metacommunity ndarray
        ## FIXME: Sometimes the logser R call returns a number of species not == to the #
        ##        requested, so this will raise. The better thing to do here would be to
        ##        fix the R code.
        try:
            self.community["abundances"] = np.array(abundances)
            self.community["ids"] = ids
            self.community['trait_values'] = np.array(trait_values)
        except ValueError as inst:
            msg = \
"""
  Attempting to set metacommunity size {} with {} species. This can happen sometimes
  with the `logser` metacommunity simulation. Simplest to ignore and rerun it.
"""
            raise MESSError(msg.format(self.paramsdict["S_m"], len(ids)))

        ## Calculate immigration probabilities
        ## Here the true Jm under the logseries model will not equal the true
        ## sum of abundances, since it is a random variable per simulation.
        Jm = np.sum(self.community["abundances"])
        LOGGER.debug("Size of metacommunity - {}".format(Jm))
        self.community['immigration_probabilities'] = self.community["abundances"]/float(Jm)
        LOGGER.debug("Metacommunity info: shape {}\n[:10] {}".format(self.community.shape, self.community[:10]))


    def update_species_pool(self, sname, trait_value):
        """ Add a new species to the species pool. This is on account of
        speciation in the local communities and we need to keep track of
        the trait values globally. New species are appended to the end
        and given dummy values for their immigration probability and regional
        abundance. There are no dynamics in the metacommunity so these
        new species will never act as colonists from the metacommunity.
        The form of this call is ugly for stupid reasons"""
        try:
            LOGGER.debug("Adding species/trait_value - {}/{}".format(sname, trait_value))
            self.community = np.hstack((self.community,\
                                        np.array([tuple([sname, 0, 0, trait_value])], dtype=METACOMMUNITY_DTYPE)))
        except Exception as inst:
            LOGGER.error("Error in Metacommunity.update_species_pool - {}".format(inst))
            LOGGER.error("sname/trait_value - {}/{}".format(sname, trait_value))
            raise


    ##################################
    ## Publicly useful methods
    ##################################
    def get_migrant(self):
        """ Return one
        """
        migrant_draw = np.random.multinomial(1, self.community["immigration_probabilities"], size=1).argmax()
        new_species = self.community["ids"][migrant_draw]
        trait_value = self.community["trait_values"][migrant_draw]

        #LOGGER.debug("Migrant idx {}\tid {}\t trait_val {}".format(migrant_draw, new_species, trait_value))
        return new_species, trait_value

    def get_nmigrants(self, nmigrants=1):
        migrants = []
        trait_vals = []
        for i in range(nmigrants):
            mig, trait = self.get_migrant()
            migrants.append(mig)
            trait_vals.append(trait)
        return migrants, trait_vals


#############################
## Metacommunity Parameter Info Dicts
#############################
LOCAL_PARAMS = {
    "S_m" : "Number of species in the regional pool",\
    "J_m" : "Total # of individuals in the regional pool",\
    "speciation_rate" : "Speciation rate of metacommunity",\
    "death_proportion" : "Proportion of speciation rate to be extinction rate",\
    "trait_rate_meta" : "Trait evolution rate parameter for metacommunity",\
    "ecological_strength" : "Strength of community assembly process on phenotypic change",\
}


if __name__ == "__main__":
    print("Test logser")
    data = Metacommunity("logser")
    print("{} {}".format(data, data.community[:10]))
    print("Test uniform")
    data = Metacommunity("uniform")
    print("{} {}".format(data, data.community[:10]))
    print("Test lognorm")
    data = Metacommunity("lognorm")
    print("{} {}".format(data, data.community[:10]))
    print("Test full file")
    data = Metacommunity("../SpInfo.txt")
    print("{} {}".format(data, data.community[:10]))

    for x in range(10):
        print(data.get_migrant())

    migs, traits = data.get_nmigrants(5)
    print(migs, traits)
