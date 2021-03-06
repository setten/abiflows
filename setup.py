#!/usr/bin/env python
"""Setup script for abipy."""
from __future__ import print_function

import sys
import os
import shutil
import numpy as np

from glob import glob
from setuptools import find_packages, setup, Extension

if sys.version[0:3] < '2.7':
    sys.stderr.write("abiflows requires Python version 2.7 or above. Exiting.")
    sys.exit(1)

#-------------------------------------------------------------------------------
# Useful globals and utility functions
#-------------------------------------------------------------------------------

# A little utility we'll need below, since glob() does NOT allow you to do exclusion on multiple endings!
def file_doesnt_end_with(test, endings):
    """
    Returns true if test is a file and its name does NOT end with any
    of the strings listed in endings.
    """
    if not os.path.isfile(test):
        return False
    for e in endings:
        if test.endswith(e):
            return False
    return True

#---------------------------------------------------------------------------
# Basic project information
#---------------------------------------------------------------------------

# release.py contains version, authors, license, url, keywords, etc.
release_file = os.path.join('abiflows', 'core', 'release.py')

with open(release_file) as f:
    code = compile(f.read(), release_file, 'exec')
    exec(code)

#---------------------------------------------------------------------------
# Find package data
#---------------------------------------------------------------------------

def find_package_data():
    """Find abiflows package_data."""
    # This is not enough for these things to appear in an sdist.
    # We need to muck with the MANIFEST to get this to work
    package_data = {'abiflows.fireworks.tasks': ['n1000multiples_primes.json']
    #    'abipy.data.refs' : [
    #        "al_g0w0_spfunc/*",
    #        "alas_phonons/*",
    #        "si_bse/*",
    #        "si_ebands/*",
    #        "si_g0w0/*",
    #        ],
    }

    return package_data


def find_exclude_package_data():
    package_data = {
    #    'abipy.data' : ["managers", 'benchmarks','runs/flow_*'],
    }
    return package_data


#---------------------------------------------------------------------------
# Find scripts
#---------------------------------------------------------------------------

def find_scripts():
    """Find abipy scripts."""
    scripts = []
    # All python files in abipy/scripts
    pyfiles = glob(os.path.join('abiflows', 'scripts', "*.py"))
    scripts.extend(pyfiles)
    return scripts


def get_long_desc():
    with open("README.md") as f:
        long_desc = f.read()
        #ind = long_desc.find("\n")
        #long_desc = long_desc[ind + 1:]
        return long_desc


#-----------------------------------------------------------------------------
# Function definitions
#-----------------------------------------------------------------------------

def cleanup():
    """Clean up the junk left around by the build process."""

    if "develop" not in sys.argv:
        try:
            shutil.rmtree('abiflows.egg-info')
        except:
            try:
                os.unlink('abiflows.egg-info')
            except:
                pass

# List of external packages we rely on.
# Note setup install will download them from Pypi if they are not available.
install_requires = [
    "six",
    "prettytable",
    "numpy",
    "python-daemon",
    "pymongo",
    "mongoengine",
    "flask",
    "flask-mongoengine",
    "fireworks",
    "paramiko"
    #"abipy",
    #"apscheduler==2.1.0",
    #"pydispatcher>=2.0.3",
    #"pyyaml>=3.11",
    #"pandas",
    #"netCDF4",
    #"html2text",
    #"pigments",
    #"jinja2",    
]

#---------------------------------------------------------------------------
# Find all the packages, package data, and data_files
#---------------------------------------------------------------------------

# Get the set of packages to be included.
my_packages = find_packages(exclude=())

my_scripts = find_scripts()

my_package_data = find_package_data()
my_excl_package_data = find_exclude_package_data()

# Create a dict with the basic information
# This dict is eventually passed to setup after additional keys are added.
setup_args = dict(
      name=name,
      version=version,
      description=description,
      long_description=long_description,
      author=author,
      author_email=author_email,
      url=url,
      license=license,
      platforms=platforms,
      keywords=keywords,
      classifiers=classifiers,
      install_requires=install_requires,
      packages=my_packages,
      package_data=my_package_data,
      exclude_package_data=my_excl_package_data,
      scripts=my_scripts,
      #download_url=download_url,
      )


if __name__ == "__main__":
    setup(**setup_args)
    cleanup()
