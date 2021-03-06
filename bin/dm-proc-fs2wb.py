#!/usr/bin/env python
"""
This will convert the all freesurfer outputs to hcp "space".

Usage:
  dm-proc-fs2wb.py [options] <fssubjectsdir> <hcpdir>

Arguments:
    <fssubjectsdir>      Path to input directory (freesurfer SUBJECTS_DIR)
    <hcpdir>             Path to top hcp directory (outputs)   `

Options:
  --prefix STR			   Tag for filtering subject directories
  --walltime TIME          A walltime to pass to qbatch [default: 2:00:00]
  --walltime-qc TIME       A walltime for the qc step [default: 2:00:00]
  -v,--verbose             Verbose logging
  --debug                  Debug logging in Erin's very verbose style
  -n,--dry-run             Dry run
  -h,--help                Print help

DETAILS
Converts freesurfer outputs to a Human Connectome Project outputs in
a rather organized way on all the participants within one project.

This script writes a little script (bin/hcpconvert.sh) within the output directory structure
that gets submitted to the queue for each subject. Subject's ID is passed into the qsub
command as an argument.

"""
from docopt import docopt
import pandas as pd
import datman as dm
import datman.utils
import datman.scanid
import glob
import os.path
import sys
import subprocess
import datetime
import tempfile
import shutil
import filecmp
import difflib

arguments       = docopt(__doc__)
inputpath       = arguments['<fssubjectsdir>']
targetpath      = arguments['<hcpdir>']
prefix          = arguments['--prefix']
walltime        = arguments['--walltime']
walltime_qc     = arguments['--walltime-qc']
VERBOSE         = arguments['--verbose']
DEBUG           = arguments['--debug']
DRYRUN          = arguments['--dry-run']

if DEBUG: print arguments

### Erin's little function for running things in the shell
def docmd(cmd):
    "sends a command (inputed as a list) to the shell"
    if DEBUG: print cmd
    rtn, out, err = dm.utils.run(cmd, dryrun = DRYRUN)

epiclone = os.path.join(os.environ['DATMAN_ASSETSDIR'],'epitome','160404-ewd')

### build a template .sh file that gets submitted to the queue
def makerunsh(filename):
    """
    builds a script in the target directory (run.sh)
    that gets submitted to the queue for each participant
    """

    bname = os.path.basename(filename)
    if bname == runconvertsh:
        thisSTEP = 'Convert'
    if bname == runpostsh:
        thisSTEP = 'Post'

    #open file for writing
    runsh = open(filename,'w')
    runsh.write('#!/bin/bash\n\n')
    runsh.write('## this script was created by dm-proc-fs2wb.py\n\n')
    runsh.write('## Prints loaded modules to the log\nmodule list\n\n')
    runsh.write('export  PATH=${{PATH}}:{}/bin\n'.format(epiclone))
    runsh.write('export  PYTHONPATH=${{PYTHONPATH}}:{}\n\n'.format(epiclone))
    runsh.write('export SUBJECTS_DIR=' + inputpath + '\n')
    runsh.write('export HCP_DATA=' + targetpath +'\n\n')

    if thisSTEP == 'Convert':
        ## add a line that will read in the subject id
        runsh.write('SUBJECT=${1}\n')

        #add a line to cd to the CIVET directory
        runsh.write('cd ${HCP_DATA}\n\n')

        ## start building the CIVET command
        runsh.write('fs2hcp '\
            ' --FSpath=${SUBJECTS_DIR} --HCPpath=${HCP_DATA} ' +\
            '--subject=${SUBJECT}')

    if thisSTEP == 'Post':
        if prefix:
            runsh.write('epi-hcp-qc --subjects-filter {} native\n'.format(prefix))
            runsh.write('epi-hcp-qc --subjects-filter {} MNIfsaverage32k\n'.format(prefix))
        else:
            runsh.write('epi-hcp-qc native\n')
            runsh.write('epi-hcp-qc MNIfsaverage32k\n')

    runsh.close()
    os.chmod(filename, 0o755)

### check the template .sh file that gets submitted to the queue to make sure option haven't changed
def checkrunsh(filename):
    """
    write a temporary (run.sh) file and than checks it againts the run.sh file already there
    This is used to double check that the pipeline is not being called with different options
    """
    tempdir = tempfile.mkdtemp()
    tmprunsh = os.path.join(tempdir,os.path.basename(filename))
    makerunsh(tmprunsh)
    if filecmp.cmp(filename, tmprunsh):
        if DEBUG: print("{} already written - using it".format(filename))
    else:
        # If the two files differ - then we use difflib package to print differences to screen
        print('#############################################################\n')
        print('# Found differences in {} these are marked with (+) '.format(filename))
        print('#############################################################')
        with open(filename) as f1, open(tmprunsh) as f2:
            differ = difflib.Differ()
            print(''.join(differ.compare(f1.readlines(), f2.readlines())))
        sys.exit("\nOld {} doesn't match parameters of this run....Exiting".format(filename))
    shutil.rmtree(tempdir)

######## NOW START the 'main' part of the script ##################
## make the civety directory if it doesn't exist
targetpath = os.path.normpath(targetpath)
logs_dir  = os.path.join(targetpath+'/logs/')
bin_dir  = os.path.join(targetpath+'/bin/')
subprocess.call(['mkdir','-p',logs_dir])
subprocess.call(['mkdir','-p',bin_dir])

# writes a standard CIVET running script for this project (if it doesn't exist)
# the script requires a $SUBJECT variable - that gets sent if by qsub (-v option)
runconvertsh = 'fs2hcprun.sh'
runpostsh ='fs2hcpqc.sh'
for runfilename in [runconvertsh, runpostsh]:
    runsh = os.path.join(bin_dir,runfilename)
    if os.path.isfile(runsh):
        ## create temporary run file and test it against the original
        checkrunsh(runsh)
    else:
        ## if it doesn't exist, write it now
        makerunsh(runsh)

####set checklist dataframe structure here
#because even if we do not create it - it will be needed for newsubs_df (line 80)
cols = ["id", "date_converted", "qc_rator", "qc_rating", "notes"]

# if the checklist exists - open it, if not - create the dataframe
checklistfile = os.path.normpath(targetpath+'/checklist.csv')
if os.path.isfile(checklistfile):
	checklist = pd.read_csv(checklistfile, sep=',', dtype=str, comment='#')
else:
	checklist = pd.DataFrame(columns = cols)


## find those subjects in input who have not been processed yet and append to checklist
subids_fs = filter(os.path.isdir, glob.glob(os.path.join(inputpath, '*')))
for i, subj in enumerate(subids_fs):
    subids_fs[i] = os.path.basename(subj)
subids_fs = [ v for v in subids_fs if "PHA" not in v ] ## remove the phantoms from the list

not_a_subid = ['logs','bin','QA','fsaverage','rh.EC_average','lh.EC_average']
for not_subid in not_a_subid:
    subids_fs = filter(lambda x: not_subid != x, subids_fs)

if prefix != None:
    subids_fs = [ v for v in subids_fs if prefix in v ] ## remove the phantoms from the list
newsubs = list(set(subids_fs) - set(checklist.id))
newsubs_df = pd.DataFrame(columns = cols, index = range(len(checklist),len(checklist)+len(newsubs)))
newsubs_df.id = newsubs
checklist = checklist.append(newsubs_df)



## now checkoutputs to see if any of them have been run
#if yes update spreadsheet
#if no submits that subject to the queue
jobnameprefix="fs2wb_{}_".format(datetime.datetime.today().strftime("%Y%m%d-%H%M%S"))
submitted = False

for i in range(0,len(checklist)):
    subid = checklist['id'][i]
    freesurferdone = os.path.join(inputpath,subid,'scripts','recon-all.done')
    # checks that all the input files are there
    FSready = os.path.exists(freesurferdone)
    FS32 = os.path.join(targetpath,subid,'MNINonLinear','fsaverage_LR32k',subid + '.aparc.32k_fs_LR.dlabel.nii')
    # if all input files are there - check if an output exists
    if not FSready or os.path.exists(FS32):
        continue

    jobname = jobnameprefix + subid
    os.chdir(bin_dir)
    docmd('echo ./{script} {subid} | '
          'qbatch -N {jobname} --logdir {logdir} --walltime {wt} -'.format(
            script = runconvertsh,
            subid = subid,
            jobname = jobname,
            logdir = logs_dir,
            wt = walltime))
    checklist['date_converted'][i] = datetime.date.today()
    submitted = True


## if any subjects have been submitted,
## submit a final job that will consolidate the resutls after they are finished

## if any subjects have been submitted,
## submit a final job that will qc the resutls after they are finished
if submitted:
    os.chdir(bin_dir)
    #if any subjects have been submitted - submit an extract consolidation job to run at the end
    docmd('echo ./{script} | '
          'qbatch -N {jobname} --logdir {logdir} --afterok {hold} --walltime {wt} -'.format(
            script = runpostsh,
            jobname = jobnameprefix + 'hcp_qc',
            logdir = logs_dir,
            hold = jobnameprefix + '*',
            wt = walltime_qc))

## write the checklist out to a file
checklist.to_csv(checklistfile, sep=',', columns = cols, index = False)
