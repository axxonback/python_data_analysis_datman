#!/usr/bin/env python
"""
Produces QC documents for each exam.

Usage:
    qc-html.py [options]

Options:
    --datadir DIR           Parent folder holding exported data [default: data]
    --qcdir DIR             Folder for QC reports [default: qc]
    --dbdir DIR             Folder for the database (default, same as --qcdir)
    --project-settings YML  File with project settings (to read expected file list from)
    --subject SCANID        Scan ID to QC for. E.g. DTI_CMH_H001_01_01
    --rewrite               Rewrite the html of an existing qc page
    --verbose               Be chatty
    --debug                 Be extra chatty
    --dry-run               Don't actually do any work

DETAILS

    This program requires the AFNI toolkit to be available, as well as NIFTI
    scans for each acquisition to be QC'd. That is, it searches for exported
    nifti acquistions in:

        <datadir>/nii/<timepoint>

    The database stores some of the numbers plotted here, and is used by web-
    build to generate interactive charts detailing the acquisitions over time.

"""

import os
import sys
import glob
import logging
import sqlite3
import datetime
import numpy as np
import scipy as sp
import scipy.signal as sig
import dicom as dcm
import nibabel as nib
import datman as dm
import datman.utils
import datman.scanid
import subprocess as proc
from copy import copy
from docopt import docopt
import re
import tempfile
import textwrap
import yaml
import pandas as pd

import matplotlib
matplotlib.use('Agg')   # Force matplotlib to not use any Xwindows backend
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages

logging.basicConfig(level=logging.WARN,
    format="[%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger(os.path.basename(__file__))

DEBUG = False
VERBOSE = False
DRYRUN = False
FIGDPI = 144
REWRITE = False

# adds qascripts to the environment
ASSETS = '{}/assets'.format(os.path.dirname(dm.utils.script_path()))
os.environ['PATH'] += os.pathsep + ASSETS + '/qascripts_version2'

class Document:
    pass

###############################################################################
# HELPERS

def makedirs(path):
    logger.debug("makedirs: {}".format(path))
    if not DRYRUN: os.makedirs(path)

def run(cmd):
    logger.debug("exec: {}".format(cmd))
    if not DRYRUN:
        p = proc.Popen(cmd, shell=True, stdout=proc.PIPE, stderr=proc.PIPE)
        out, err = p.communicate()
        if p.returncode != 0:
            logger.error("Error {} while executing: {}".format(p.returncode, cmd))
            out and logger.error("stdout: \n>\t{}".format(out.replace('\n','\n>\t')))
            err and logger.error("stderr: \n>\t{}".format(err.replace('\n','\n>\t')))
        else:
            logger.debug("rtnval: {}".format(p.returncode))
            out and logger.debug("stdout: \n>\t{}".format(out.replace('\n','\n>\t')))
            err and logger.debug("stderr: \n>\t{}".format(err.replace('\n','\n>\t')))

def create_db(cur):
    cur.execute('CREATE TABLE fmri (subj TEXT, site TEXT)')
    cur.execute('CREATE TABLE dti (subj TEXT, site TEXT)')
    cur.execute('CREATE TABLE t1 (subj TEXT, site TEXT)')

def found_files_df(config, scanpath, subject):
    '''
    reads in the export info from the config file and
    compares it to the contents of the subjects nii folder (scanpath)
    write the results out info a pandas dataframe
    '''
    ## get a list of files
    allfiles = []
    for filetype in ('*.nii.gz', '*.nii'):
        allfiles.extend(glob.glob(scanpath + '/*' + filetype))
    allbfiles = []
    for file in allfiles: allbfiles.append(os.path.basename(file))

    ### initialize the DataFrame
    cols = ['tag', 'File','bookmark', 'Note']
    exportinfo = pd.DataFrame(columns=cols)
    idx = 0

    ### for the subjects site - compare filelist to exportinfo
    for sitedict in config['Sites']:
        site = sitedict.keys()[0]
        if site in subject:
            for row in sitedict[site]['ExportInfo']:
                tag = row.keys()[0]
                expected_count = row[tag]['Count']
                tagstring = "_{}_".format(tag)
                bfiles = [k for k in allbfiles if tagstring in k]
                bfiles.sort()
                filenum = 1
                for bfile in bfiles:
                    bookmark = tag + str(filenum)
                    notes='Repeated Scan' if filenum > expected_count else ''
                    exportinfo.loc[idx] = [tag, bfile, bookmark, notes]
                    idx += 1
                    filenum += 1
                if filenum < (expected_count + 1):
                    notes='missing({})'.format(expected_count-filenum + 1)
                    exportinfo.loc[idx] = [tag, '', '', notes]
                    idx += 1

    ## add any extra files to the end
    ## need to add PD and T2 as expected files...
    exportinfoFiles = exportinfo.File.tolist()
    PDT2scans = [k for k in exportinfoFiles if '_PDT2_' in k]
    if len(PDT2scans) > 0:
        for PDT2scan in PDT2scans:
            exportinfoFiles.append(PDT2scan.replace('_PDT2_','_T2_'))
            exportinfoFiles.append(PDT2scan.replace('_PDT2_','_PD_'))

    otherscans = list(set(allbfiles) - set(exportinfoFiles))
    for oscan in otherscans:
        exportinfo.loc[idx] = ['unknown', oscan, '', 'extra scan']
        idx += 1

    return(exportinfo)

def qchtml_writetable(qchtml, exportinfo):
    ##write table header
    qchtml.write('<table>'
                '<tr><th>Tag</th>'
                '<th>File</th>'
                '<th>Notes</th></tr>')

    ## for each row write the table data
    for row in range(0,len(exportinfo)):
        qchtml.write('<tr><td>{}</td>'.format(exportinfo.loc[row,'tag'])) ## table new row
        qchtml.write('<td><a href="#{}">{}</a></td>'.format(exportinfo.loc[row,'bookmark'],exportinfo.loc[row,'File']))
        qchtml.write('<td><font color="#FF0000">{}</font></td></tr>'.format(exportinfo.loc[row,'Note'])) ## table new row

    ##end table
    qchtml.write('</table>\n')

def nifti_basename(fpath):
    """
    return basename with out .nii.gz extension
    """
    basefpath = os.path.basename(fpath)
    stem = basefpath.replace('.nii.gz','')
    return(stem)

def insert_value(cur, table, subj, colname, value):
    """
    Insets values into the database (differently for numeric and string data).
    """

    # check if column exits, add if it does not
    cur.execute('PRAGMA table_info({})'.format(table))
    d = cur.fetchall()

    cols = []
    for col in d:
        cols.append(str(col[1]))

    if colname not in cols:
        cur.execute('ALTER TABLE {table} ADD COLUMN {colname} FLOAT DEFAULT null'.format(
                           table=table, colname=colname))

    # check if subject exists
    cur.execute("""SELECT * FROM {table} WHERE subj='{subj}'""".format(
                       table=table, subj=subj))
    d = cur.fetchall()

    # if subject does not exist, insert row
    if len(d) == 0:
        if type(value) == str:
            cur.execute("""INSERT INTO {table}(subj, {colname}) VALUES('{subj}', '{value}')""".format(
                           table=table, subj=subj, colname=colname, value=value))
        else:
            cur.execute("""INSERT INTO {table}(subj, {colname}) VALUES('{subj}', {value})""".format(
                           table=table, subj=subj, colname=colname, value=value))
    # otherwise, update row
    else:
        if type(value) == str:
            cur.execute("""UPDATE {table} SET {colname} = '{value}' WHERE subj='{subj}'""".format(
                           table=table, subj=subj, colname=colname, value=value))
        else:
            cur.execute("""UPDATE {table} SET {colname} = {value} WHERE subj='{subj}'""".format(
                           table=table, subj=subj, colname=colname, value=value))

def factors(n):
    """
    Returns all factors of n.
    """
    return set(reduce(list.__add__,
                ([i, n//i] for i in range(1, int(n**0.5) + 1) if n % i == 0)))

def square_factors(fac, num):
    """
    Finds the two most square factors of a number from a list of factors.
    Factors returned with the smallest first.
    """
    candidates = []
    for x in fac:
        for y in fac:
            if x*y == num:
                candidates.append(abs(x-y))
    most_square = np.min(candidates)
    for x in fac:
        for y in fac:
            if x*y == num:
                if x-y == most_square:
                    factor = [x, y]
    if factor[0] > factor[1]:
        factor = factor[::-1]

    return factor

def add_pic_to_html(qchtml, pic):
    '''
    Adds a pic to an html page with this handler "qchtml"
    '''
    relpath = os.path.relpath(pic,os.path.dirname(qchtml.name))
    qchtml.write('<a href="'+ relpath + '" >')
    qchtml.write('<img src="' + relpath + '" > ')
    qchtml.write('</a><br>\n')
    return qchtml

def fslslicer_pic(fpath,pic,slicergap,picwidth):
    """
    Uses FSL's slicer function to generate a pretty montage png from a nifti file
    Then adds a link to that png in the qcthml

    Usage:
        add_slicer_pic(fpath,slicergap,picwidth,qchtml)

        fpath       -- submitted image file name
        slicergap   -- int of "gap" between slices in Montage
        picwidth    -- width (in pixels) of output image
        pic         -- fullpath to for output image
    """
    run("slicer {} -S {} {} {}".format(fpath,slicergap,picwidth,pic))

def load_masked_data(func, mask):
    """
    Accepts 'functional.nii.gz' and 'mask.nii.gz', and returns a voxels x
    timepoints matrix of the functional data in non-zero mask locations.
    """
    func = nib.load(func).get_data()
    mask = nib.load(mask).get_data()

    mask = mask.reshape(mask.shape[0]*mask.shape[1]*mask.shape[2])
    func = func.reshape(func.shape[0]*func.shape[1]*func.shape[2],
                                                    func.shape[3])

    # find within-brain timeseries
    idx = np.where(mask > 0)[0]
    func = func[idx, :]

    return func

def check_n_trs(fpath):
    """
    Returns the number of TRs for an input file. If the file is 3D, we also
    return 1.
    """
    data = nib.load(fpath)

    try:
        ntrs = data.shape[3]
    except:
        ntrs = 1

    return ntrs

def bounding_box(filename):
    """
    Finds a box that only includes all nonzero voxels in a 3D image. Output box
    is represented as 3 x 2 numpy array with rows denoting x, y, z, and columns
    denoting stand and end slices.

    Usage:
        box = bounding_box(filename)
    """

    # find 3D bounding box
    box = np.zeros((3,2))  # init bounding box
    flag = 0  # ascending

    for i, dim in enumerate(filename.shape): # loop through (x, y, z)

        # ascending search
        while flag == 0:
            for dim_test in np.arange(dim):

                # get sum of all values in each slice
                if i == 0:   test = np.sum(filename[dim_test, :, :])
                elif i == 1: test = np.sum(filename[:, dim_test, :])
                elif i == 2: test = np.sum(filename[:, :, dim_test])

                # if slice is nonzero, set starting bound, switch to descending
                if test >= 1:
                    box[i, 0] = dim_test
                    flag = 1
                    break

        # descending search
        while flag == 1:
            for dim_test in np.arange(dim):

                dim_test = dim-dim_test - 1  # we have to reverse things

                # get sum of all values in each slice
                if i == 0:   test = np.sum(filename[dim_test, :, :])
                elif i == 1: test = np.sum(filename[:, dim_test, :])
                elif i == 2: test = np.sum(filename[:, :, dim_test])

                # if slice is nonzero, set ending bound, switch to ascending
                if test >= 1:
                    box[i, 1] = dim_test
                    flag = 0
                    break

    return box

def reorient_4d_image(image):
    """
    Reorients the data to radiological, one TR at a time
    """
    for i in np.arange(image.shape[3]):

        if i == 0:
            newimage = np.transpose(image[:, :, :, i], (2,0,1))
            newimage = np.rot90(newimage, 2)

        elif i == 1:
            tmpimage = np.transpose(image[:, :, :, i], (2,0,1))
            tmpimage = np.rot90(tmpimage, 2)
            newimage = np.concatenate((newimage[...,np.newaxis],
                                       tmpimage[...,np.newaxis]), axis=3)

        else:
            tmpimage = np.transpose(image[:, :, :, i], (2,0,1))
            tmpimage = np.rot90(tmpimage, 2)
            newimage = np.concatenate((newimage,
                                       tmpimage[...,np.newaxis]), axis=3)

    image = copy(newimage)

    return image

###############################################################################
# PLOTTERS / CALCULATORS

def montage(image, name, filename, pic, cmaptype='grey', mode='3d', minval=None, maxval=None, box=None):
    """
    Creates a montage of images displaying a image set on top of a grayscale
    image.

    Generally, this will be used to plot an image (of type 'name') that was
    generated from the original file 'filename'. So if we had an SNR map
    'SNR.nii.gz' from 'fMRI.nii.gz', we would submit everything to montage
    as so:

        montage('SNR.nii.gz', 'SNR', 'EPI.nii.gz', 'EPI_SNR.png')

    Usage:
        montage(image, name, filename, pic)

        image    -- submitted image file name
        name     -- name of the printout (e.g, SNR map, t-stats, etc.)
        cmaptype -- 'redblue', 'hot', or 'gray'.
        minval   -- colormap minimum value as a % (None == 'auto')
        maxval   -- colormap maximum value as a % (None == 'auto')
        mode     -- '3d' (prints through space) or '4d' (prints through time)
        filename -- qc image file name
        picpath  -- Path to save the figure .png to
        box      -- a (3,2) tuple that describes the start and end voxel
                    for x, y, and z, respectively. If None, we find it ourselves.
    """
    image = str(image) # input checks
    opath = os.path.dirname(image) # grab the image folder
    output = str(image)
    image = nib.load(image).get_data() # load in the daterbytes

    if mode == '3d':
        if len(image.shape) > 3: # if image is 4D, only keep the first time-point
            image = image[:, :, :, 0]

        image = np.transpose(image, (2,0,1))
        image = np.rot90(image, 2)
        steps = np.round(np.linspace(0,np.shape(image)[0]-2, 36)) # coronal plane
        factor = 6

        # use bounding box (submitted or found) to crop extra-brain regions
        if box == None:
            box = bounding_box(image) # get the image bounds
        elif box.shape != (3,2): # if we did, ensure it is the right shape
            logger.error('ERROR: Bounding box should have shape = (3,2).')
            raise ValueError
        image = image[box[0,0]:box[0,1], box[1,0]:box[1,1], box[2,0]:box[2,1]]

    if mode == '4d':
        image = reorient_4d_image(image)
        midslice = np.floor((image.shape[2]-1)/2) # print a single plane across all slices
        factor = np.ceil(np.sqrt(image.shape[3])) # print all timepoints
        factor = factor.astype(int)

    # colormapping -- set value
    if cmaptype == 'redblue': cmap = plt.cm.RdBu_r
    elif cmaptype == 'hot': cmap = plt.cm.OrRd
    elif cmaptype == 'gray': cmap = plt.cm.gray
    else:
        logger.debug('No valid colormap supplied, default = greyscale.')
        cmap = plt.cm.gray

    # colormapping -- set range
    if minval == None:
        minval = np.min(image)
    else:
        minval = np.min(image) + ((np.max(image) - np.min(image)) * minval)

    if maxval == None:
        maxval = np.max(image)
    else:
        maxval = np.max(image) * maxval

    cmap.set_bad('g', 0)  # value for transparent pixels in the overlay

    fig, axes = plt.subplots(nrows=factor, ncols=factor, facecolor='white')
    for i, ax in enumerate(axes.flat):

        if mode == '3d':
            im = ax.imshow(image[steps[i], :, :], cmap=cmap, interpolation='nearest', vmin=minval, vmax=maxval)
            ax.set_frame_on(False)
            ax.axes.get_xaxis().set_visible(False)
            ax.axes.get_yaxis().set_visible(False)

        elif mode == '4d' and i < image.shape[3]:
            im = ax.imshow(image[:, :, midslice, i], cmap=cmap, interpolation='nearest')
            ax.set_frame_on(False)
            ax.axes.get_xaxis().set_visible(False)
            ax.axes.get_yaxis().set_visible(False)

        elif mode == '4d' and i >= image.shape[3]:
            ax.set_axis_off() # removes extra axes from plot

    plt.subplots_adjust(left=0, right=0.85, top=0.9, bottom=0)

    cbar_ax = fig.add_axes([0.88, 0.10, 0.05, 0.7])
    cb = fig.colorbar(im, cax=cbar_ax)
    fig.suptitle(filename + '\n' + name, size=10)

    fig.savefig(pic, format='png', dpi=FIGDPI)
    plt.close()

def find_epi_spikes(image, filename, pic, ftype, cur=None, bvec=None):

    """
    Plots, for each axial slice, the mean instensity over all TRs.
    Strong deviations are an indication of the presence of spike
    noise.

    If bvec is supplied, we remove all time points that are 0 in the bvec
    vector.

    Usage:
        find_epi_spikes(image, filename, picpath)

        image    -- submitted image file name
        filename -- qc image file name
        pic      -- path to save the .png figure to
        ftype    -- 'fmri' or 'dti'
        cur      -- cursor object for subject qc database (if None, don't use)
        bvec     -- numpy array of bvecs (for finding direction = 0)

    """

    image = str(image)             # input checks
    opath = os.path.dirname(image) # grab the image folder

    # load in the daterbytes
    output = str(image)
    image = nib.load(image).get_data()
    image = reorient_4d_image(image)

    x = image.shape[1]
    y = image.shape[2]
    z = image.shape[0]
    t = image.shape[3]

    # initialize the spikecount
    spikecount = 0

    # find the most square set of factors for n_trs
    factor = np.ceil(np.sqrt(z))
    factor = factor.astype(int)

    fig, axes = plt.subplots(nrows=factor, ncols=factor, facecolor='white')

    # sets the bounds of the image
    c1 = np.round(x*0.25)
    c2 = np.round(x*0.75)

    # for each axial slice
    for i, ax in enumerate(axes.flat):
        if i < z:

            v_mean = np.array([])
            v_sd = np.array([])

            # find the mean, STD, of each dir and concatenate w. vector
            for j in np.arange(t):

                # gives us a subset of the image
                sample = image[i, c1:c2, c1:c2, j]
                mean = np.mean(sample)
                sd = np.std(sample)

                if j == 0:
                    v_mean = copy(mean)
                    v_sd = copy(sd)
                else:
                    v_mean = np.hstack((v_mean, mean))
                    v_sd = np.hstack((v_sd, sd))

            # crop out b0 images
            if bvec is None:
                v_t = np.arange(t)
            else:
                idx = np.where(bvec != 0)[0]
                v_mean = v_mean[idx]
                v_sd = v_sd[idx]
                v_t = np.arange(len(idx))

            # keep track of spikes
            v_spikes = np.where(v_mean > np.mean(v_mean)+np.mean(v_sd))[0]
            spikecount = spikecount + len(v_spikes)

            ax.plot(v_mean, color='black')
            ax.fill_between(v_t, v_mean-v_sd, v_mean+v_sd, alpha=0.5, color='black')
            ax.set_frame_on(False)
            ax.axes.get_xaxis().set_visible(False)
            ax.axes.get_yaxis().set_visible(False)
        else:
            ax.set_axis_off()

    if cur:
        subj = filename.split('_')[0:4]
        subj = '_'.join(subj)

        insert_value(cur, ftype, subj, 'spikecount', spikecount)

    plt.subplots_adjust(left=0, right=1, top=0.9, bottom=0)
    plt.suptitle('{}\nDTI Slice/TR Wise Abnormalities'.format(filename), size=10)


    fig.savefig(pic, format='png', dpi=FIGDPI)
    plt.close()

def fmri_plots(func, mask, f, filename, pic, cur=None):
    """
    Calculates and plots:
         + Mean and SD of normalized spectra across brain.
         + Framewise displacement (mm/TR) of head motion.
         + Mean correlation from 10% of the in-brain voxels.
         + EMPTY ADD KEWL PLOT HERE PLZ.

    """
    ##############################################################################
    # spectra
    plt.subplot(2,2,1)
    func = load_masked_data(func, mask)
    spec = sig.detrend(func, type='linear')
    spec = sig.periodogram(spec, fs=0.5, return_onesided=True, scaling='density')
    freq = spec[0]
    spec = spec[1]
    sd = np.nanstd(spec, axis=0)
    mean = np.nanmean(spec, axis=0)

    plt.plot(freq, mean, color='black', linewidth=2)
    plt.plot(freq, mean + sd, color='black', linestyle='-.', linewidth=0.5)
    plt.plot(freq, mean - sd, color='black', linestyle='-.', linewidth=0.5)
    plt.title('Whole-brain spectra mean, SD', size=6)
    plt.xticks(size=6)
    plt.yticks(size=6)
    plt.xlabel('Frequency (Hz)', size=6)
    plt.ylabel('Power', size=6)
    plt.xticks([])

    ##############################################################################
    # framewise displacement
    plt.subplot(2,2,2)
    fd_thresh = 0.5
    f = np.genfromtxt(f)
    f[:,0] = np.radians(f[:,0]) * 50 # 50 = head radius, need not be constant.
    f[:,1] = np.radians(f[:,1]) * 50 # 50 = head radius, need not be constant.
    f[:,2] = np.radians(f[:,2]) * 50 # 50 = head radius, need not be constant.
    f = np.abs(np.diff(f, n=1, axis=0))
    f = np.sum(f, axis=1)
    t = np.arange(len(f))

    plt.plot(t, f.T, lw=1, color='black')
    plt.axhline(y=fd_thresh, xmin=0, xmax=len(t), color='r')
    plt.xlim((-3, len(t) + 3)) # this is in TRs
    plt.ylim(0, 2) # this is in mm/TRs
    plt.xticks(size=6)
    plt.yticks(size=6)
    plt.xlabel('TR', size=6)
    plt.ylabel('Framewise displacement (mm/TR)', size=6)
    plt.title('Head motion', size=6)

    if cur:
        fdtot = np.sum(f) # total framewise displacement
        fdnum = len(np.where(f > fd_thresh)[0]) # number of TRs above 0.5 mm FD

        subj = filename.split('_')[0:4]
        subj = '_'.join(subj)

        insert_value(cur, 'fmri', subj, 'fdtot', fdtot)
        insert_value(cur, 'fmri', subj, 'fdnum', fdnum)

    ##############################################################################
    # whole brain correlation
    plt.subplot(2,2,3)
    idx = np.random.choice(func.shape[0], func.shape[0]/10, replace=False)
    corr = func[idx, :]
    corr = sp.corrcoef(corr, rowvar=1)
    mean = np.mean(corr, axis=None)
    std = np.std(corr, axis=None)

    im = plt.imshow(corr, cmap=plt.cm.RdBu_r, interpolation='nearest', vmin=-1, vmax=1)
    plt.xlabel('Voxel', size=6)
    plt.ylabel('Voxel', size=6)
    plt.xticks([])
    plt.yticks([])
    cb = plt.colorbar(im)
    cb.set_label('Correlation (r)', labelpad=0, y=0.5, size=6)
    for tick in cb.ax.get_yticklabels():
        tick.set_fontsize(6)
    plt.title('Whole-brain r mean={}, SD={}'.format(str(mean), str(std)), size=6)

    if cur:
        subj = filename.split('_')[0:4]
        subj = '_'.join(subj)

        insert_value(cur, 'fmri', subj, 'corrmean', mean)
        insert_value(cur, 'fmri', subj, 'corrsd', std)

    plt.suptitle(filename)
    plt.savefig(pic, format='png', dpi=FIGDPI)
    plt.close()

###############################################################################
# PIPELINES

def ignore(fpath, qcpath, qchtml, cur):
    pass

def fmri_qc(fpath, qcpath, qchtml, cur):
    """
    This takes an input image, motion corrects, and generates a brain mask.
    It then calculates a signal to noise ratio map and framewise displacement
    plot for the file.

    At the moment, the only difference between this and fmri_qc is that this
    also adds some stats to the subject-qc database.
    """
    # if the number of TRs is too little, we skip the pipeline
    ntrs = check_n_trs(fpath)

    if ntrs < 20:
        return
    
    filename = os.path.basename(fpath)
    filestem = nifti_basename(fpath)
    tmpdir = tempfile.mkdtemp(prefix='qc-')

    run('3dvolreg \
         -prefix {t}/mcorr.nii.gz \
         -twopass -twoblur 3 -Fourier \
         -1Dfile {t}/motion.1D {f}'.format(t=tmpdir, f=fpath))
    run('3dTstat -prefix {t}/mean.nii.gz {t}/mcorr.nii.gz'.format(t=tmpdir))
    run('3dAutomask \
         -prefix {t}/mask.nii.gz \
         -clfrac 0.5 -peels 3 {t}/mean.nii.gz'.format(t=tmpdir))
    run('3dTstat -prefix {t}/std.nii.gz  -stdev {t}/mcorr.nii.gz'.format(t=tmpdir))
    run("""3dcalc \
           -prefix {t}/sfnr.nii.gz \
           -a {t}/mean.nii.gz -b {t}/std.nii.gz -expr 'a/b'""".format(t=tmpdir))

    # output BOLD-contrast qc-pic
    BOLDpic = os.path.join(qcpath, filestem + '_BOLD.png')
    montage(fpath, 'BOLD-contrast', filename, BOLDpic, maxval=0.75)
    add_pic_to_html(qchtml, BOLDpic)

    # output fMRI plots
    fMRIplotspic = os.path.join(qcpath,filestem + '_fmriplots.png')
    fmri_plots('{t}/mcorr.nii.gz'.format(t=tmpdir),
                     '{t}/mask.nii.gz'.format(t=tmpdir),
                     '{t}/motion.1D'.format(t=tmpdir), filename, fMRIplotspic, cur)
    add_pic_to_html(qchtml, fMRIplotspic)

    SNRpic = os.path.join(qcpath,filestem + '_SNR.png')
    montage('{t}/sfnr.nii.gz'.format(t=tmpdir),
                  'SFNR', filename, SNRpic, cmaptype='hot', maxval=0.75)
    add_pic_to_html(qchtml, SNRpic)


    Spikespic = os.path.join(qcpath,filestem + '_Spikes.png')
    find_epi_spikes(fpath, filename, Spikespic, 'fmri', cur=cur)
    add_pic_to_html(qchtml, Spikespic)

    # run metrics from qascripts toolchain
    run('ln -s {fpath} {t}/fmri.nii.gz'.format(fpath=fpath, t=tmpdir))
    run('qa_bold_v2.sh {t}/fmri.nii.gz {t}/qc_fmri.csv'.format(t=tmpdir))
    run('mv {t}/qc_fmri.csv {qcpath}/{filestem}_qascript_fmri.csv'.format(t=tmpdir, filestem=filestem, qcpath=qcpath))

    run('rm -r {}'.format(tmpdir))

def rest_qc(fpath, qcpath, qchtml, cur):
    """
    This takes an input image, motion corrects, and generates a brain mask.
    It then calculates a signal to noise ratio map and framewise displacement
    plot for the file.
    """
    fmri_qc(fpath, qcpath, qchtml, cur)

def pdt2_qc(fpath,qcpath, qchtml, cur):
    ## split PD and T2 image
    pdpath = fpath.replace('_PDT2_','_PD_')
    t2path = fpath.replace('_PDT2_','_T2_')
    pd_qc(pdpath,qcpath, qchtml, cur)
    t2_qc(t2path,qcpath, qchtml, cur)

def t1_qc(fpath, qcpath, qchtml, cur):
    pic=os.path.join(qcpath, nifti_basename(fpath) + '.png')
    fslslicer_pic(fpath,pic,5,1600)
    add_pic_to_html(qchtml, pic)

def pd_qc(fpath,qcpath, qchtml, cur):
    pic=os.path.join(qcpath, nifti_basename(fpath) + '.png')
    fslslicer_pic(fpath,pic,2,1600)
    add_pic_to_html(qchtml, pic)

def t2_qc(fpath, qcpath, qchtml, cur):
    pic=os.path.join(qcpath, nifti_basename(fpath) + '.png')
    fslslicer_pic(fpath,pic,2,1600)
    add_pic_to_html(qchtml, pic)

def flair_qc(fpath,qcpath, qchtml, cur):
    pic=os.path.join(qcpath, nifti_basename(fpath) + '.png')
    fslslicer_pic(fpath,pic,2,1600)
    add_pic_to_html(qchtml, pic)

def dti_qc(fpath, qcpath, qchtml, cur):
    """
    Runs the QC pipeline on the DTI inputs. We use the BVEC (not BVAL)
    file to find B0 images (in some scans, mid-sequence B0s are coded
    as non-B0s for some reason, so the 0-direction locations in BVEC
    seem to be the safer choice).
    """
    filename = os.path.basename(fpath)
    filestem = nifti_basename(fpath)
    directory = os.path.dirname(fpath)
    bvecfile = fpath[:-len(datman.utils.get_extension(fpath))] + ".bvec"
    bvalfile = fpath[:-len(datman.utils.get_extension(fpath))] + ".bval"

    # load in bvec file
    logger.debug("fpath = {}, bvec = {}".format(fpath, bvecfile))

    if not os.path.exists(bvecfile):
        logger.warn("Expected bvec file not found: {}. Skipping".format(bvecfile))
        return

    bvec = np.genfromtxt(bvecfile)
    bvec = np.sum(bvec, axis=0)

    B0pic = os.path.join(qcpath,filestem + '_B0.png')
    montage(fpath, 'B0-contrast', filename, B0pic, maxval=0.25)
    add_pic_to_html(qchtml, B0pic)

    dti4dpic = os.path.join(qcpath,filestem + '_dti4d.png')
    montage(fpath, 'DTI Directions', filename, dti4dpic, mode='4d', maxval=0.25)
    add_pic_to_html(qchtml, dti4dpic)

    spikespic = os.path.join(qcpath, filestem + '_spikes.png')
    find_epi_spikes(fpath, filename, spikespic, 'dti', cur=cur, bvec=bvec)
    add_pic_to_html(qchtml, spikespic)

    # run metrics from qascripts toolchain
    tmpdir = tempfile.mkdtemp(prefix='qc-')
    run('ln -s {fpath} {t}/dti.nii.gz'.format(fpath=fpath, t=tmpdir))
    run('ln -s {bvecfile} {t}/dti.bvec'.format(bvecfile=bvecfile, t=tmpdir))
    run('ln -s {bvalfile} {t}/dti.bval'.format(bvalfile=bvalfile, t=tmpdir))
    run('qa_dti_v2.sh {t}/dti.nii.gz {t}/dti.bval {t}/dti.bvec {t}/qc_dti.csv'.format(t=tmpdir))
    run('mv {t}/qc_dti.csv {qcpath}/{filestem}_qascript_dti.csv'.format(t=tmpdir, filestem=filestem, qcpath=qcpath))

    run('rm -r {}'.format(tmpdir))

def add_header_checks(fpath, qchtml, logdata):
    filestem = os.path.basename(fpath).replace(dm.utils.get_extension(fpath),'')
    lines = [re.sub('^.*?: *','',line) for line in logdata if filestem in line]
    if not lines:
        return

    qchtml.write('<h3> {} header differences </h3>\n<table>'.format(filestem))
    for l in lines:
        qchtml.write('<tr><td>{}</td></tr>'.format(l))
    qchtml.write('</table>\n')

def add_bvec_checks(fpath, qchtml, logdata):
    filestem = os.path.basename(fpath).replace(dm.utils.get_extension(fpath),'')
    lines = [re.sub('^.*'+filestem,'',line) for line in logdata if filestem in line]
    if not lines:
        return

    #text ='\n'.join(['\n'.join(textwrap.wrap(l,width=120,subsequent_indent=" "*4)) for l in lines])

    qchtml.write('<h3> {} bvec/bval differences </h3>\n<table>'.format(filestem))
    for l in lines:
        qchtml.write('<tr><td>{}</td></tr>'.format(l))
    qchtml.write('</table>\n')
    
def add_old_image(fpath, qcpath, qchtml, tag):
    fname = nifti_basename(fpath)
    fname = os.path.join(qcpath, fname)
    
    if os.path.exists(fname + '.png'):
        add_pic_to_html(qchtml, fname + '.png')
        return
    elif os.path.exists(fname + '_BOLD.png'):
        add_pic_to_html(qchtml, fname + '_BOLD.png')
        add_pic_to_html(qchtml, fname + '_fmriplots.png')
        add_pic_to_html(qchtml, fname + '_SNR.png')
        add_pic_to_html(qchtml, fname + '_Spikes.png')
    elif os.path.exists(fname + '_B0.png'):
        add_pic_to_html(qchtml, fname + '_B0.png')
        add_pic_to_html(qchtml, fname + '_dti4d.png')
        add_pic_to_html(qchtml, fname + '_spikes.png')
    elif tag == 'PDT2':
        add_pic_to_html(qchtml, fname.replace('_PDT2_','_PD_') + '.png')
        add_pic_to_html(qchtml, fname.replace('_PDT2_', '_T2_') + '.png')
        
        
###############################################################################
# MAIN

def qc_folder(scanpath, subject, qcdir, cur, pconfig, QC_HANDLERS):
    """
    QC all the images in a folder (scanpath).

    Outputs PDF and other files to outputdir. All files named startng with
    subject.

    'cur' is a cursor pointing to the QC database.

    pconfig is loaded from the project_settings.yml file
    """

    qcdir = dm.utils.define_folder(qcdir)
    qcpath = dm.utils.define_folder(os.path.join(qcdir,subject))

    htmlfile = os.path.join(qcpath, 'qc_{}.html'.format(subject))
    if os.path.exists(htmlfile) and not REWRITE:
        logger.debug("{} exists, skipping.".format(htmlfile))
        return

    if REWRITE:
        try: 
            os.remove(htmlfile)
        except:
            print("{} does not exist. Reconstructing html file from any images present.".format(htmlfile))

    qchtml = open(htmlfile,'a')
    qchtml.write('<HTML><TITLE>{} qc</TITLE>\n'.format(subject))
    qchtml.write('<head>\n<style>\n'
                'body { font-family: futura,sans-serif;'
                '        text-align: center;}\n'
                'img {width:90%; \n'
                '   display: block\n;'
                '   margin-left: auto;\n'
                '   margin-right: auto }\n'
                'table { margin: 25px auto; \n'
                '        border-collapse: collapse;\n'
                '        text-align: left;\n'
                '        width: 90%; \n'
                '        border: 1px solid grey;\n'
                '        border-bottom: 2px solid black;} \n'
                'th {background: black;\n'
                '    color: white;\n'
                '    text-transform: uppercase;\n'
                '    padding: 10px;}\n'
                'td {border-top: thin solid;\n'
                '    border-bottom: thin solid;\n'
                '    padding: 10px;}\n'
                '</style></head>\n')

    qchtml.write('<h1> QC report for {} <h1/>'.format(subject))

    # add in sites to the database
    insert_value(cur, 'fmri', subject, 'site', subject.split('_')[1])
    insert_value(cur, 'dti', subject, 'site', subject.split('_')[1])
    insert_value(cur, 't1', subject, 'site', subject.split('_')[1])

    ## now read exportinfo from config_yml
    exportinfo = found_files_df(pconfig, scanpath, subject)
    qchtml_writetable(qchtml, exportinfo)

    ## hack job - find and add a link to the technotes..
    ## find the tech notes - if this is CMH
    if 'CMH' in subject:
        techglob = '{}/../../RESOURCES/{}*/*/*/*.pdf'.format(scanpath,subject)
        technotes = glob.glob(techglob)

        if len(technotes) > 0:
            techrelpath = os.path.relpath(
                                os.path.abspath(technotes[0]),
                                os.path.dirname(qchtml.name))
            qchtml.write('<a href="'+ techrelpath + '" >')
            qchtml.write('Click Here to open Tech Notes')
            qchtml.write('</a><br>\n')
        else:
            qchtml.write('<p>Tech Notes not found</p>\n')

    # load up any header/bvec check log files for the subjectt
    header_check_logs = glob.glob(os.path.join(
                         qcdir, 'logs', 'dm-check-headers-{}*'.format(subject)))
    header_check_log = []
    for logfile in header_check_logs:
        header_check_log += open(logfile).readlines()

    # load up any header/bvec check log files for the subjectt
    bvecs_check_logs = glob.glob(os.path.join(
                           qcdir, 'logs', 'dm-check-bvecs-{}*'.format(subject)))
    bvecs_check_log = []
    for logfile in bvecs_check_logs:
        bvecs_check_log += open(logfile).readlines()

    for idx in range(0,len(exportinfo)):
        bname = exportinfo.loc[idx,'File']
        if bname!='' :
            fname = os.path.join(scanpath, bname)
            logger.info("QC scan {}".format(fname))
            ident, tag, series, description = dm.scanid.parse_filename(fname)
            qchtml.write('<h2 id="{}">{}</h2>\n'.format(exportinfo.loc[idx,'bookmark'], bname))
            if tag not in QC_HANDLERS:
                logger.info("MSG: No QC tag {} for scan {}. Skipping.".format(
                                                                    tag, fname))
                continue
            if header_check_log and tag!='PDT2':
                add_header_checks(fname, qchtml, header_check_log)
            if bvecs_check_log:
                add_bvec_checks(fname, qchtml, bvecs_check_log)
                
            if not REWRITE:    
                QC_HANDLERS[tag](fname, qcpath, qchtml, cur)
            else:
                add_old_image(fname, qcpath, qchtml, tag)
                
            qchtml.write('<br>')

    qchtml.close()

def main():
    """
    This spits out our QCed data
    """
    global VERBOSE
    global DEBUG
    global DRYRUN
    global REWRITE

    QC_HANDLERS = {   # map from tag to QC function
            "T1"            : t1_qc,
            "T2"            : t2_qc,
            "PD"            : pd_qc,
            "PDT2"          : pdt2_qc,
            "FLAIR"         : flair_qc,
            "FMAP"          : ignore,
            "FMAP-6.5"      : ignore,
            "FMAP-8.5"      : ignore,
            "RST"           : rest_qc,
            "EPI"           : fmri_qc,
            "SPRL"          : rest_qc,
            "OBS"           : fmri_qc,
            "IMI"           : fmri_qc,
            "NBK"           : fmri_qc,
            "EMP"           : fmri_qc,
            "VN-SPRL"       : fmri_qc,
            "SID"           : fmri_qc,
            "MID"           : fmri_qc,
            "DTI"           : dti_qc,
            "DTI21"         : dti_qc,
            "DTI22"         : dti_qc,
            "DTI23"         : dti_qc,
            "DTI60-29-1000" : dti_qc,
            "DTI60-20-1000" : dti_qc,
            "DTI60-1000"    : dti_qc,
            "DTI60-b1000"   : dti_qc,
            "DTI33-1000"    : dti_qc,
            "DTI33-b1000"   : dti_qc,
            "DTI33-3000"    : dti_qc,
            "DTI33-b3000"   : dti_qc,
            "DTI33-4500"    : dti_qc,
            "DTI33-b4500"   : dti_qc,

    }
    arguments = docopt(__doc__)
    datadir   = arguments['--datadir']
    qcdir     = arguments['--qcdir']
    dbdir     = arguments['--dbdir']
    ymlfile   = arguments['--project-settings']
    scanid    = arguments['--subject']
    REWRITE   = arguments['--rewrite']
    VERBOSE   = arguments['--verbose']
    DEBUG     = arguments['--debug']
    DRYRUN    = arguments['--dry-run']

    if VERBOSE:
        logging.getLogger().setLevel(logging.INFO)
    if DEBUG:
        logging.getLogger().setLevel(logging.DEBUG)

    if scanid:
        timepoint_glob = '{}/nii/{}'.format(datadir, scanid)
    else:
        timepoint_glob = '{}/nii/*'.format(datadir)

    if not dbdir: dbdir = qcdir
    db_filename = '{}/subject-qc.db'.format(dbdir)
    db_is_new = not os.path.exists(db_filename)

    try:
        db = sqlite3.connect(db_filename)
    except:
        logger.error('Invalid database path, or permissions issue.')
        sys.exit('Invalid database path, or permissions issue.')

    cur = db.cursor()

    # initialize the tables if the database does not yet exist
    if db_is_new == True:
        create_db(cur)

    # load the yml of project settings
    with open(ymlfile, 'r') as stream:
        pconfig = yaml.load(stream)

    for path in glob.glob(timepoint_glob):
        subject = os.path.basename(path)

        # skip phantoms
        if 'PHA' in subject:
            pass
        else:
            logger.info("QCing folder {}".format(path))
            qc_folder(path, subject, qcdir, cur, pconfig, QC_HANDLERS)

    # close database properly
    cur.close()
    db.close()

if __name__ == "__main__":
    main()
