#!/usr/bin/env python
"""
Produces or modifies a checklist.csv file that summmarizes the QC .pdf
reports generated by the qc.py pipeline.

Usage:
    qc-report.py [options] <project>

Arguments:
    <project>           Full path to the project directory containing data/.

Options:
    --qcdir PATH       Full path to qc directory
    --checklist FILE   The checklist file to update
    --verbose          Be chatty
    --debug            Be extra chatty

DETAILS

    This finds all human .pdfs in qc/, figures out which of these are
    not currently in checklist.csv, and adds their names to the list
    of scans.

    checklist.csv is a space-delimited file that has a column containing
    the pdf documents, as well as a place for people to mark that they
    have reviewed them.

    This message is printed with the -h, --help flags.
"""

import os
import glob
import datman as dm
import datman.utils
import datman.scanid
from docopt import docopt

VERBOSE = False
DRYRUN  = False
DEBUG   = False

def parse_checklist(base_path, checklist):
    """
    Looks for the checklist file. If it does not exist, we don't do anything.
    If it does, we keep a list of the already-entered scans so we only append
    scans we haven't QCed yet.
    """

    # if the checklist does not exist yet, we dont need to do anything
    if os.path.isfile(checklist) == False:
        return None

    scans = []
    with open(checklist) as f:
        for line in f:
            line = line.strip()
            scanname = line.split(' ')[0]
            if scanname: scans.append(scanname)

    return scans

def get_qc(base_path):
    """
    Gets all of the human qc .pdf files in qc/, and returns them as a list.
    """
    files = os.listdir(base_path)
    files = filter(lambda x: '.pdf' in x, files)
    files = filter(lambda x: 'PHA' not in x, files)

    ## now also searches for html qc pages
    filesb = glob.glob(base_path + '/*/qc*html')
    basefilesb = [os.path.basename(x) for x in filesb]
    files.extend(basefilesb)

    return files

def main():

    arguments = docopt(__doc__)
    project   = arguments['<project>']
    qcdir     = arguments['--qcdir']
    checklist = arguments['--checklist']
    VERBOSE   = arguments['--verbose']
    DEBUG     = arguments['--debug']

    if not checklist:
        checklist = os.path.join(project, 'metadata/checklist.csv')

    if not qcdir:
        qcdir = os.path.join(project, 'qc')
    # finds all of the .pdfs already in the checklist
    scans = parse_checklist(project, checklist)

    # gets a list of all the unposted pdfs
    files = get_qc(qcdir)
    newfiles = []
    if scans != None:
        scanstems = [os.path.splitext(x)[0] for x in scans]
        for file in files:
            filestem = os.path.splitext(file)[0]
            if filestem not in scanstems: newfiles.append(file)

    newfiles.sort()

    with open(checklist, "a") as f:
        for fname in newfiles:
            f.write(fname + ' \n')

    print('Added {} qc reports to {}'.format(len(newfiles), checklist))

if __name__ == '__main__':
    main()
