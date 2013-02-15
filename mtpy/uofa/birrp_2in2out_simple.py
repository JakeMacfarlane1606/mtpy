#!/usr/bin/env python
"""
This is a convenience script for running BIRRP. 

arguments:
birrp executable, stationname (uppercase), directory containing time series files, coherence threshold

A subfolder 'birrp_processed' for the output is generated within the time series directory 

"""


import numpy as np
import re
import sys, os
import glob
import os.path as op
import glob
import calendar
import time


from mtpy.utils.exceptions import *

import mtpy.processing.birrp as BP
reload(BP)


def main():

    if len(sys.argv) < 4:
        raise MTpyError_inputarguments('Need at least 3 arguments: <path to BIRRP executable> <station name> <directory for time series>')

    try:
        coherence_th = float(sys.argv[4])
        if not 0 < coherence_th <= 1: 
            raise
    except: 
        print 'coherence value invalid (float from interval ]0,1]) - set to 0.5 instead'
        coherence_th = 0.5

    birrp_exe_raw = sys.argv[1] 
    birrp_exe = op.abspath(op.realpath(birrp_exe_raw))

    if not op.isfile(birrp_exe):
        raise MTpyError_inputarguments('Birrp executable not existing: %s' % (birrp_exe))

    stationname = sys.argv[2].upper()

    ts_dir_raw = sys.argv[3]
    ts_dir = op.abspath(op.realpath(ts_dir_raw))


    if not op.isdir(ts_dir):
        raise MTpyError_inputarguments('Time series directory not existing: %s' % (ts_dir))

    BP.runbirrp2in2out_simple(birrp_exe, stationname, ts_dir, coherence_th)



if __name__=='__main__':
    main()