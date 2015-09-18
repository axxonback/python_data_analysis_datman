#!/usr/bin/env python
"""
Creates a subject list yaml file for QAP from a dataman export folder

Usage: 
    qap_subjectlist.py <datadir> <output.yaml>
    qap_subjectlist.py <datadir> persubject <outputdir>

Arguments: 
    <datadir>        The top-level datman folder for nifti exports. This path
                     should contain a folder for each subject timepoint, and
                     within that scan files. For example: 
                     
                       <datadir>/DTI_CMH_H001_01/DTI_CMH_H001_01_01_T1_01_descr.nii.gz
                       <datadir>/DTI_CMH_H040_01/DTI_CMH_H040_01_01_T1_02_descr.nii.gz
                       <datadir>/DTI_CMH_H040_02/DTI_CMH_H040_01_02_T1_02_descr.nii.gz
                     
    <output.yaml>    The subject list output file.

    <outputdir>      The directory to output per-subject subject list files.
                     This is useful for parallelising QAP jobs over individual
                     subjects.  Outputs are named after the subject.
"""

import docopt
import glob
import yaml
import datman
import os.path
import collections

# A dictionary mapping datman image kinds to QAP image kinds
IMAGETYPES = {
        'T1' : 'anatomical_scan', 
        'RST': 'functional_scan',
        }

def main(): 
    arguments = docopt.docopt(__doc__)
    datadir = arguments['<datadir>']
    outputfile = arguments['<output.yaml>']
    outputdir = arguments['<outputdir>']

    # kickflip to create a recursive defaultdict, and register it with pyyaml
    tree = lambda: collections.defaultdict(tree)
    yaml.add_representer(collections.defaultdict, yaml.representer.Representer.represent_dict)

    subjectlist = tree() 

    for path in glob.glob(os.path.join(datadir, '*', '*.nii.gz')): 
        filename  = os.path.basename(path)

        try: 
            scanid, kind, series, description = datman.scanid.parse_filename(filename)
            if kind not in IMAGETYPES.keys(): continue
        except datman.scanid.ParseException, e: 
            continue

        subjectid = scanid.get_full_subjectid()
        filestem  = filename.replace(".nii.gz","")

        subjectlist[subjectid][scanid.timepoint][IMAGETYPES[kind]][filestem] = path

    if arguments['persubject']:
        for subject, data in subjectlist.items():
            outputfile = open(os.path.join(outputdir, subject + '.yaml'), 'w')
            outputfile.write(yaml.dump({subject:data}, default_flow_style = False))
    else:
        open(outputfile, 'w').write(yaml.dump(subjectlist, default_flow_style=False))

if __name__ == '__main__':
    main()
