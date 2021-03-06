
import argparse
import astropy.io.fits as pyfits
from astropy.io import ascii
from astropy.stats import sigma_clipped_stats
from astropy.wcs import WCS
import numpy as np
#import numpy.fft as fft
import matplotlib
import matplotlib.pyplot as plt
import os
from subprocess import call
from scipy import ndimage
import time
# these are important to speed up the FFTs
import pyfftw
import pyfftw.interfaces.numpy_fft as fft
pyfftw.interfaces.cache.enable()
#pyfftw.interfaces.cache.set_keepalive_time(1.)

import sys
sys.path.append('/Users/pmv/Python/cosmics.py_0.4')
import cosmics

from sip_to_pv import *

# some global parameter settings

# optimal subtraction parameters
subimage_size = 1024     # size of subimages
subimage_border = 28     # border around subimage to avoid edge effects
background_sex = False   # background: use Sextractor image or simple median 
addfakestar = False      # add a fake star at the centre of every subimage
fratio_local = True      # determine fratio (Fn/Fr) from subimage
dxdy_local = True        # determine dx and dy (sigma_x and sigma_y) from subimage

# switch on/off different functions
docosmics = False        # remove cosmic rays
dosex = False            # do extra SExtractor run (already done inside Astrometry.net)
dosex_psffit = False     # do extra SExtractor run with PSF fitting

# general
redo = True             # execute functions even if output file exist
verbose = False          # print out extra info
timing = True            # (wall-)time the different functions
display = False          # show intermediate images
makeplots = False        # produce astrometry plots

pixelscale = 0.4

def optimal_subtraction(new_fits, ref_fits):
    
    """Function that accepts a new and a reference fits image, finds their
    WCS solution using Astrometry.net, runs SExtractor (inside
    Astrometry.net), PSFex to extract the PSF from the images, and
    performs Barak's optimal subtraction to produce the subtracted
    image (D), the significance image (S), and the corrected
    significance image (Scorr - see Zackay, Ofek & Gal-Yam 2016, ApJ,
    830, 27).

    Requirements:
    - Astrometry.net (in particular "solve-field" and index files)
    - SExtractor
    - SWarp
    - PSFex
    - ds9
    - pyfftw to speed up the many FFTs performed
    - the other modules imported at the top
 
    Written by Paul Vreeswijk (pmvreeswijk@gmail.com)

    """

    start_time1 = os.times()

    # define the base names of input fits files, base_new and
    # base_ref, as global so they can be used in any function in this
    # module
    global base_new, base_ref
    base_new = new_fits[0:-5]
    base_ref = ref_fits[0:-5]
    
    # read in header of new_fits
    t = time.time()
    with pyfits.open(new_fits) as hdulist:
        header_new = hdulist[0].header
    keywords = ['NAXIS2','NAXIS1','GAIN','RDNOISE','SATURATE','RA','DEC']
    ysize, xsize, gain_new, readnoise_new, satlevel_new, ra_new, dec_new = read_header(header_new, keywords)
    if verbose:
        print keywords
        print read_header(header_new, keywords)

    # read in header of ref_fits
    with pyfits.open(ref_fits) as hdulist:
        header_ref = hdulist[0].header
    ysize_ref, xsize_ref, gain_ref, readnoise_ref, satlevel_ref, ra_ref, dec_ref = read_header(header_ref, keywords)
    if verbose:
        print keywords
        print read_header(header_ref, keywords)

        
    if docosmics:
        # clean new image of cosmic rays
        new_fits_crr = base_new+'_crr.fits'
        new_fits_crrmask = base_new+'_crrmask.fits'
        if not os.path.isfile(new_fits_crr) or redo:
            result = clean_cosmics(new_fits, new_fits, new_fits_crrmask, 
                                   gain_new, readnoise_new, 5.0, 0.3, 5.0, 4, -1., False)
            
        # clean ref image of cosmic rays
        ref_fits_crr = base_ref+'_crr.fits'
        ref_fits_crrmask = base_ref+'_crrmask.fits'
        if not os.path.isfile(ref_fits_crr) or redo:
            result = clean_cosmics(ref_fits, ref_fits, ref_fits_crrmask, 
                                   gain_ref, readnoise_ref, 5.0, 0.3, 5.0, 4, -1., False)

            
    # determine WCS solution of new_fits
    new_fits_wcs = base_new+'_wcs.fits'
    if not os.path.isfile(new_fits_wcs) or redo:
        result = run_wcs(base_new+'.fits', new_fits_wcs, ra_new, dec_new,
                         gain_new, readnoise_new)
                        

    # determine WCS solution of ref_fits
    ref_fits_wcs = base_ref+'_wcs.fits'
    if not os.path.isfile(ref_fits_wcs) or redo:
        result = run_wcs(base_ref+'.fits', ref_fits_wcs, ra_ref, dec_ref,
                         gain_ref, readnoise_ref)


    # remap ref to new
    ref_fits_remap = base_ref+'_wcs_remap.fits'
    if not os.path.isfile(ref_fits_remap) or redo:
        result = run_remap(base_new+'_wcs.fits', base_ref+'_wcs.fits', ref_fits_remap,
                           [ysize, xsize], gain=gain_new, config='Config/swarp.config')

            
    # initialize full output images
    data_D_full = np.ndarray((ysize, xsize), dtype='float32')
    data_S_full = np.ndarray((ysize, xsize), dtype='float32')
    data_Scorr_full = np.ndarray((ysize, xsize), dtype='float32')
    if addfakestar:
        data_new_full = np.ndarray((ysize, xsize), dtype='float32')
        data_ref_full = np.ndarray((ysize, xsize), dtype='float32')

    # determine cutouts
    centers, cuts_ima, cuts_ima_fft, cuts_fft, sizes = centers_cutouts(subimage_size, ysize, xsize)
    ysize_fft = subimage_size + 2*subimage_border
    xsize_fft = subimage_size + 2*subimage_border
    nsubs = centers.shape[0]
    if verbose:
        print 'nsubs', nsubs
        for i in range(nsubs):
            print 'i', i
            print 'cuts_ima[i]', cuts_ima[i]
            print 'cuts_ima_fft[i]', cuts_ima_fft[i]
            print 'cuts_fft[i]', cuts_fft[i]
            
    # prepare cubes with shape (nsubs, ysize_fft, xsize_fft) with new,
    # ref, psf and background images
    data_new, psf_new, psf_orig_new, data_new_bkg = prep_optimal_subtraction(base_new+'_wcs.fits',
                                                                            nsubs, 'new')
    data_ref, psf_ref, psf_orig_ref, data_ref_bkg = prep_optimal_subtraction(base_ref+'_wcs.fits',
                                                                            nsubs, 'ref')

    # determine corresponding variance images
    var_new = data_new + readnoise_new**2 
    var_ref = data_ref + readnoise_ref**2
    
    if verbose:
        print 'readnoise_new, readnoise_ref', readnoise_new, readnoise_ref

    # get x, y and fratios from matching PSFex stars across entire frame
    x_fratio, y_fratio, fratio, dra, ddec = get_fratio_radec(base_new+'_wcs.psfexcat',
                                                             base_ref+'_wcs.psfexcat',
                                                             base_new+'_wcs.sexcat',
                                                             base_ref+'_wcs.sexcat')
    dx = dra / pixelscale
    dy = ddec / pixelscale
    
    dr = np.sqrt(dx**2 + dy**2)
    if verbose: print 'standard deviation dr over the full frame:', np.std(dr) 
    dr_full = np.sqrt(np.median(dr)**2 + np.std(dr)**2)
    dx_full = np.sqrt(np.median(dx)**2 + np.std(dx)**2)
    dy_full = np.sqrt(np.median(dy)**2 + np.std(dy)**2)
    #dr_full = np.std(dr)
    #dx_full = np.std(dx)
    #dy_full = np.std(dy)
    if verbose:
        print 'np.median(dr), np.std(dr)', np.median(dr), np.std(dr)
        print 'np.median(dx), np.std(dx)', np.median(dx), np.std(dx)
        print 'np.median(dy), np.std(dy)', np.median(dy), np.std(dy)
        print 'dr_full, dx_full, dy_full', dr_full, dx_full, dy_full
    
    #fratio_median, fratio_std = np.median(fratio), np.std(fratio)
    fratio_mean, fratio_median, fratio_std = sigma_clipped_stats(fratio, sigma=2.)
    if verbose:
        print 'fratio_mean, fratio_median, fratio_std', fratio_mean, fratio_median, fratio_std
    
    if makeplots:
        # plot dy vs dx
        plt.axis((-1,1,-1,1))
        plt.plot(dx, dy, 'go') 
        plt.xlabel('dx (pixels)')
        plt.ylabel('dy (pixels)')
        plt.title(new_fits+'\n vs '+ref_fits, fontsize=12)
        plt.savefig('dxdy.png')
        plt.show()
        plt.close()
        
        # plot dr vs x_fratio
        plt.axis((0,xsize,0,1))
        plt.plot(x_fratio, dr, 'go')
        plt.xlabel('x (pixels)')
        plt.ylabel('dr (pixels)')
        plt.title(new_fits+'\n vs '+ref_fits, fontsize=12)
        plt.savefig('drx.png')
        plt.show()
        plt.close()

        # plot dr vs y_fratio
        plt.axis((0,ysize,0,1))
        plt.plot(y_fratio, dr, 'go')
        plt.xlabel('y (pixels)')
        plt.ylabel('dr (pixels)')
        plt.title(new_fits+'\n vs '+ref_fits, fontsize=12)
        plt.savefig('dry.png')
        plt.show()
        plt.close()

        # plot dx vs x_fratio
        plt.axis((0,xsize,-1,1))
        plt.plot(x_fratio, dx, 'go')
        plt.xlabel('x (pixels)')
        plt.ylabel('dx (pixels)')
        plt.title(new_fits+'\n vs '+ref_fits, fontsize=12)
        plt.savefig('dxx.png')
        plt.show()
        plt.close()

        # plot dy vs y_fratio
        plt.axis((0,ysize,-1,1))
        plt.plot(y_fratio, dy, 'go')
        plt.xlabel('y (pixels)')
        plt.ylabel('dy (pixels)')
        plt.title(new_fits+'\n vs '+ref_fits, fontsize=12)
        plt.savefig('dyy.png')
        plt.show()
        plt.close()


    start_time2 = os.times()
            
    for nsub in range(nsubs):

        if timing: tloop = time.time()
    
        if verbose:
            print '\nNsub:', nsub+1
            print '----------'

        # determine clipped mean, median and stddev
        #mean_new, median_new, stddev_new = sigma_clipped_stats(data_new[nsub], sigma=3.)
        #print 'mean_new, median_new, stddev_new', mean_new, median_new, stddev_new
        median_new = np.median(data_new[nsub])
        stddev_new = np.sqrt(median_new + readnoise_new**2)
        if verbose:
            print 'median_new, stddev_new', median_new, stddev_new

        #mean_ref, median_ref, stddev_ref = sigma_clipped_stats(data_ref[nsub], sigma=3.)
        #print 'mean_ref, median_ref, stddev_ref', mean_ref, median_ref, stddev_ref
        median_ref = np.median(data_ref[nsub])
        stddev_ref = np.sqrt(median_ref + readnoise_ref**2)
        if verbose:
            print 'median_ref, stddev_ref', median_ref, stddev_ref

        show = False
        if makeplots and show:
            print 'data_new[nsub] data type:', data_new[nsub].dtype
            range_new = (median_new-3.*stddev_new, median_new+3.*stddev_new)
            bins = np.linspace(range_new[0], range_new[1], 100)
            plt.hist(np.ravel(data_new[nsub]), bins, color='green') 
            plt.xlabel('pixel value (e-)')
            plt.ylabel('number')
            plt.title('subsection of '+new_fits)
            plt.show()
            plt.close()

            print 'data_ref[nsub] data type:', data_ref[nsub].dtype
            range_ref = (median_ref-3.*stddev_ref, median_ref+3.*stddev_ref)
            bins = np.linspace(range_ref[0], range_ref[1], 100)
            plt.hist(np.ravel(data_ref[nsub]), bins, color='green') 
            plt.xlabel('pixel value (e-)')
            plt.ylabel('number')
            plt.title('subsection of '+ref_fits)
            plt.show()
            plt.close()
            
        # replace low values in subimages
        data_new[nsub][data_new[nsub] <= 0.] = median_new
        data_ref[nsub][data_ref[nsub] <= 0.] = median_ref

        # replace low values in variance subimages
        #var_new[nsub][var_new[nsub] < stddev_new**2] = stddev_new**2
        #var_ref[nsub][var_ref[nsub] < stddev_ref**2] = stddev_ref**2

        if addfakestar:
            # add fake star to new image
            # first normalize psf_orig_new
            psf_orig_new[nsub] /= np.amax(psf_orig_new[nsub]) 
            psf_orig_new[nsub] *= 3.*stddev_new
            # place it at the center of the new image
            xpos = xsize_fft/2
            ypos = ysize_fft/2
            data_new[nsub][ypos-50/2:ypos+50/2,xpos-50/2:xpos+50/2] += psf_orig_new[nsub]

        if background_sex:
            # use background subimages
            bkg_new = data_new_bkg[nsub]
            bkg_ref = data_ref_bkg[nsub]
        else:
            # or median values of subimages
            bkg_new = median_new
            bkg_ref = median_ref

        # subtract the background
        data_new[nsub] -= bkg_new
        data_ref[nsub] -= bkg_ref

        # replace saturated pixel values with zero
        #data_new[nsub][data_new[nsub] > 0.95*satlevel_new] = 0.
        #data_ref[nsub][data_ref[nsub] > 0.95*satlevel_ref] = 0.

        # get median fratio from PSFex stars across subimage
        subcut = cuts_ima[nsub]
        index_sub = ((y_fratio > subcut[0]) & (y_fratio < subcut[1]) & 
                     (x_fratio > subcut[2]) & (x_fratio < subcut[3]))

        # take local or full-frame values for fratio
        if fratio_local and any(index_sub):
            #fratio_mean, f_new, f_new_std = sigma_clipped_stats(fratio[index_sub], sigma=2.5)
            f_new, f_new_std = np.median(fratio[index_sub]), np.std(fratio[index_sub])
        else:
            f_new, f_new_std = fratio_median, fratio_std
        # and the same for dx and dy
        if dxdy_local and any(index_sub):
            dx_sub = np.sqrt(np.median(dx[index_sub])**2 + np.std(dx[index_sub])**2)
            dy_sub = np.sqrt(np.median(dy[index_sub])**2 + np.std(dy[index_sub])**2)
            if dx_sub > 2.*dx_full or not np.isfinite(dx_sub):
                dx_sub = dx_full
            if dy_sub > 2.*dy_full or not np.isfinite(dy_sub):
                dy_sub = dy_full
        else:
            dx_sub = dx_full
            dy_sub = dy_full

        # f_ref is set to one - could also opt to set f_new to unity instead
        f_ref = 1.
        if verbose:
            print 'f_new, f_new_std, f_ref', f_new, f_new_std, f_ref
            print 'dx_sub, dy_sub', dx_sub, dy_sub


        # call Barak's function: optimal_binary_image_subtraction
        data_D, data_S, data_Scorr = run_ZOGY(data_ref[nsub], data_new[nsub], 
                                              psf_ref[nsub], psf_new[nsub], 
                                              stddev_ref, stddev_new, 
                                              f_ref, f_new,
                                              var_ref[nsub], var_new[nsub],
                                              dx_sub, dy_sub)

        # check that robust stddev of Scorr is around unity
        if verbose:
            mean_Scorr, median_Scorr, stddev_Scorr = sigma_clipped_stats(data_Scorr, sigma=3.)
            print 'mean_Scorr, median_Scorr, stddev_Scorr', mean_Scorr, median_Scorr, stddev_Scorr

        # put sub images into output frames
        subcut = cuts_ima[nsub]
        fftcut = cuts_fft[nsub]
        y1 = subimage_border
        x1 = subimage_border
        y2 = subimage_border+subimage_size
        x2 = subimage_border+subimage_size
        data_D_full[subcut[0]:subcut[1],subcut[2]:subcut[3]] = data_D[y1:y2,x1:x2] / gain_ref
        data_S_full[subcut[0]:subcut[1],subcut[2]:subcut[3]] = data_S[y1:y2,x1:x2]
        data_Scorr_full[subcut[0]:subcut[1],subcut[2]:subcut[3]] = data_Scorr[y1:y2,x1:x2]

        if addfakestar:
            if background_sex:
                data_new_full[subcut[0]:subcut[1],subcut[2]:subcut[3]] = (data_new[nsub][y1:y2,x1:x2]
                                                                          + bkg_new[y1:y2,x1:x2]) / gain_new
                data_ref_full[subcut[0]:subcut[1],subcut[2]:subcut[3]] = (data_ref[nsub][y1:y2,x1:x2]
                                                                          + bkg_ref[y1:y2,x1:x2]) / gain_ref
            else:
                data_new_full[subcut[0]:subcut[1],subcut[2]:subcut[3]] = (data_new[nsub][y1:y2,x1:x2]
                                                                          + bkg_new) / gain_new
                data_ref_full[subcut[0]:subcut[1],subcut[2]:subcut[3]] = (data_ref[nsub][y1:y2,x1:x2]
                                                                          + bkg_ref) / gain_ref
        
        if display and (nsub == 65 or nsub==0):
            # just for displaying purpose:
            pyfits.writeto('D.fits', data_D, clobber=True)
            pyfits.writeto('S.fits', data_S, clobber=True)
            pyfits.writeto('Scorr.fits', data_Scorr, clobber=True)
            #pyfits.writeto('Scorr_1sigma.fits', data_Scorr_1sigma, clobber=True)
        
            # write new and ref subimages to fits
            subname = '_sub'+str(nsub)
            newname = base_new+'_wcs'+subname+'.fits'
            pyfits.writeto(newname, data_new[nsub]+bkg_new, clobber=True)
            refname = base_ref+'_wcs'+subname+'.fits'
            pyfits.writeto(refname, data_ref[nsub]+bkg_ref, clobber=True)
            # variance images
            pyfits.writeto('Vnew.fits', var_new[nsub], clobber=True)
            pyfits.writeto('Vref.fits', var_ref[nsub], clobber=True)
            
            # and display
            cmd = ['ds9','-zscale',newname,refname,'D.fits','S.fits','Scorr.fits']
            cmd = ['ds9','-zscale',newname,refname,'D.fits','S.fits','Scorr.fits',
                   'Vnew.fits', 'Vref.fits', 'VSn.fits', 'VSr.fits', 
                   'VSn_ast.fits', 'VSr_ast.fits', 'Sn.fits', 'Sr.fits', 'kn.fits', 'kr.fits']
            result = call(cmd)

        if timing: print 'wall-time spent in nsub loop', time.time()-tloop
            
    end_time = os.times()
    dt_usr  = end_time[2] - start_time2[2]
    dt_sys  = end_time[3] - start_time2[3]
    dt_wall = end_time[4] - start_time2[4]
    print
    print "Elapsed user time in {0}:  {1:.3f} sec".format("optsub", dt_usr)
    print "Elapsed CPU time in {0}:  {1:.3f} sec".format("optsub", dt_sys)
    print "Elapsed wall time in {0}:  {1:.3f} sec".format("optsub", dt_wall)
        
    dt_usr  = end_time[2] - start_time1[2]
    dt_sys  = end_time[3] - start_time1[3]
    dt_wall = end_time[4] - start_time1[4]
    print
    print "Elapsed user time in {0}:  {1:.3f} sec".format("total", dt_usr)
    print "Elapsed CPU time in {0}:  {1:.3f} sec".format("total", dt_sys)
    print "Elapsed wall time in {0}:  {1:.3f} sec".format("total", dt_wall)

    # write full new, ref, D and S images to fits
    if addfakestar:
        pyfits.writeto('new.fits', data_new_full, header_new, clobber=True)
        pyfits.writeto('ref.fits', data_ref_full, header_ref, clobber=True)
    pyfits.writeto('D.fits', data_D_full, clobber=True)
    pyfits.writeto('S.fits', data_S_full, clobber=True)
    pyfits.writeto('Scorr.fits', data_Scorr_full, clobber=True)
    
    # and display
    if addfakestar:
        cmd = ['ds9','-zscale','new.fits','ref.fits','D.fits','S.fits','Scorr.fits']
    else:
        cmd = ['ds9','-zscale',new_fits,ref_fits_remap,'D.fits','S.fits','Scorr.fits']
    result = call(cmd)

def read_header(header, keywords):

    values = []
    for i in range(len(keywords)):
        values.append(header[keywords[i]])
    return values

    
def prep_optimal_subtraction(input_fits, nsubs, imtype):

    print '\nexecuting prep_optimal_subtraction ...'
    t = time.time()
    
    # read in header and data

    # in case of the reference image, the image before remapping 
    # should be read
    read_fits = input_fits
    if imtype == 'ref':
        read_fits = input_fits.replace('.fits', '_remap.fits')
    with pyfits.open(read_fits) as hdulist:
        header = hdulist[0].header
        data = hdulist[0].data
    # get gain and readnoise from header
    gain = header['GAIN']
    readnoise = header['RDNOISE']
    # convert counts to electrons
    data *= gain

    # determine psf of input image with get_psf function
    psf, psf_orig = get_psf(input_fits, header, nsubs, imtype)

    # read background image produced by sextractor
    if background_sex:
        bkg_fits = input_fits.replace('_wcs.fits', '_bkg.fits')
        with pyfits.open(bkg_fits) as hdulist:
            data_bkg = hdulist[0].data
        # convert counts to electrons
        data_bkg *= gain
    else:
        # return zero array with same shape as data
        # 
        data_bkg = np.zeros(data.shape)
        
    # split full image into subimages
    print 'image shape', data.shape
    ysize, xsize = header['NAXIS2'], header['NAXIS1']
    # determine cutouts
    centers, cuts_ima, cuts_ima_fft, cuts_fft, sizes = centers_cutouts(subimage_size, ysize, xsize)
    ysize_fft = subimage_size + 2*subimage_border
    xsize_fft = subimage_size + 2*subimage_border
    
    fftdata = np.zeros((nsubs, ysize_fft, xsize_fft), dtype='float32')
    fftdata_bkg = np.zeros((nsubs, ysize_fft, xsize_fft), dtype='float32')
    for nsub in range(nsubs):
        subcutfft = cuts_ima_fft[nsub]
        fftcut = cuts_fft[nsub]
        fftdata[nsub][fftcut[0]:fftcut[1],fftcut[2]:fftcut[3]] = data[subcutfft[0]:subcutfft[1],
                                                                      subcutfft[2]:subcutfft[3]]
        fftdata_bkg[nsub][fftcut[0]:fftcut[1],fftcut[2]:fftcut[3]] = data_bkg[subcutfft[0]:subcutfft[1],
                                                                            subcutfft[2]:subcutfft[3]]
        
    if timing: print 'wall-time spent in prep_optimal_subtraction', time.time()-t

    return fftdata, psf, psf_orig, fftdata_bkg
    

def clean_cosmics(fits_in, fits_clean, fits_mask, gain, readnoise, sigclip, 
                  sigfrac, objlim, maxiter, satlevel, verbose):

    t = time.time()
    print '\nexecuting clean_cosmics ...'

    # Read the FITS :
    array, header = cosmics.fromfits(fits_in)
    # array is a 2D numpy array

    # Build the object :
    result = cosmics.cosmicsimage(array, gain=gain, readnoise=readnoise, 
                                  sigclip=sigclip, sigfrac=sigfrac, objlim=objlim, 
                                  satlevel=satlevel, verbose=verbose)
    
    # Run the full artillery :
    result.run(maxiter=maxiter)
    
    # Write the cleaned and mask image to FITS files, conserving the original header:
    cosmics.tofits(fits_clean, result.cleanarray, header)
    cosmics.tofits(fits_mask, result.mask, header)
                           
    if timing: print 'wall-time spent in clean_cosmics', time.time()-t

    return result.cleanarray
                           

def get_psf(image, ima_header, nsubs, imtype):

    """Function that takes in [image] and determines the actual Point
    Spread Function as a function of position from the full frame,
    and returns a cube containing the psf for each subimage in the 
    full frame."""

    if timing: t = time.time()
    print '\nexecuting get_psf ...'
    
    # determine image size from header
    xsize, ysize = ima_header['NAXIS1'], ima_header['NAXIS2'] 

    # run sextractor on image; this step is no longer needed as it is
    # done inside Astrometry.net, producing the same catalog was an
    # independent SExtractor run would.
    sexcat = image.replace('.fits', '.sexcat')
    if (not os.path.isfile(sexcat) or redo) and dosex:
        result = run_sextractor(image, sexcat, 'Config/sex.config',
                                'Config/sex.params')
        
    # run psfex on SExtractor output catalog
    psfexcat = image.replace('.fits', '.psfexcat')
    if not os.path.isfile(psfexcat) or redo:
        print 'sexcat', sexcat
        print 'psfexcat', psfexcat
        result = run_psfex(sexcat, 'Config/psfex.config', psfexcat)

    # again run SExtractor, but now using output PSF from PSFex, so
    # that PSF-fitting can be performed for all objects. The output
    # columns defined in Config/sex_psffit.params include several new
    # columns related to the PSF fitting.
    if dosex_psffit:
        result = run_sextractor(image, sexcat+'_psffit', 'Config/sex_psffit.config',
                                'Config/sex_psffit.params', fitpsf=True)

    # read in PSF output binary table from psfex
    psfex_bintable = image.replace('.fits', '.psf')
    with pyfits.open(psfex_bintable) as hdulist:
        header = hdulist[1].header
        data = hdulist[1].data[0][0][:]

    # data still needs to be corrected for relative shift between new and ref
        
    # read in some header keyword values
    polzero1 = header['POLZERO1']
    polzero2 = header['POLZERO2']
    polscal1 = header['POLSCAL1']
    polscal2 = header['POLSCAL2']
    poldeg = header['POLDEG1']
    psf_fwhm = header['PSF_FWHM']
    psf_samp = header['PSF_SAMP']
    if verbose:
        print 'polzero1                   ', polzero1
        print 'polscal1                   ', polscal1
        print 'polzero2                   ', polzero2
        print 'polscal2                   ', polscal2
        print 'order polynomial:          ', poldeg
        print 'PSF FWHM:                  ', psf_fwhm
        print 'PSF sampling size (pixels):', psf_samp

    # call centers_cutouts to determine centers
    # and cutout regions of the full image
    centers, cuts_ima, cuts_ima_fft, cuts_fft, sizes = centers_cutouts(subimage_size, ysize, xsize)
    ysize_fft = subimage_size + 2*subimage_border
    xsize_fft = subimage_size + 2*subimage_border

    if imtype == 'ref':

        # in case of the ref image, the PSF was determined from the
        # original image, while it will be applied to the remapped ref
        # image. So the centers of the cutouts in the remapped ref
        # image need to be mapped back to those in the original
        # reference image to get the PSF from the proper
        # coordinates. Easiest to do this using astropy.wcs, which
        # would also take care of any potential rotation and scaling.

        # first infer ra, dec corresponding to x, y pixel positions
        # (centers[:,1] and centers[:,0], respectively, using the
        # [new].wcs file from Astrometry.net
        wcs = WCS(base_new+'.wcs')
        ra_temp, dec_temp = wcs.all_pix2world(centers[:,1], centers[:,0], 1)
        # then convert ra, dec back to x, y in the original ref image
        wcs = WCS(base_ref+'.wcs')
        centers[:,1], centers[:,0] = wcs.all_world2pix(ra_temp, dec_temp, 1)
        
    # initialize output PSF array
    psf_ima_center = np.ndarray((nsubs,ysize_fft,xsize_fft), dtype='float32')
    psf_ima_shift = np.ndarray((nsubs,ysize_fft,xsize_fft), dtype='float32')
    psf_ima_orig = np.ndarray((nsubs,50,50), dtype='float32')
    
    # loop through nsubs and construct psf at the center of each
    # subimage, using the output from PSFex that was run on the full
    # image
    for nsub in range(nsubs):
        
        x = (centers[nsub,1] - polzero1) / polscal1
        y = (centers[nsub,0] - polzero2) / polscal2

        if nsubs==1:
            psf_ima = data[0]
        else:
            if poldeg==2:
                psf_ima = data[0] + data[1] * x + data[2] * x**2 + \
                          data[3] * y + data[4] * x * y + data[5] * y**2
            elif poldeg==3:
                psf_ima = data[0] + data[1] * x + data[2] * x**2 + data[3] * x**3 + \
                          data[4] * y + data[5] * x * y + data[6] * x**2 * y + \
                          data[7] * y**2 + data[8] * x * y**2 + data[9] * y**3

        if display:
            # write this psf to fits
            pyfits.writeto('psf_'+imtype+'_sub'+str(nsub)+'.fits', psf_ima, clobber=True)
            #result = show_image(psf_ima)

        # resample PSF image at original pixel scale
        #
        # N.B.: for the moment it is assumed that both the original image
        # (image) and the PSF image (psf_ima_resized) are even in both
        # dimensions. For the PSF image this is done by forcing the
        # psfex.config paramater PSF_SAMPLING to be 2.0. Print a warning
        # message if this is not the case. The PSF image is also assumed
        # to be square.
        #
        psf_ima_resized = ndimage.zoom(psf_ima, psf_samp)
        psf_ima_orig[nsub] = psf_ima_resized
        psf_size = psf_ima_resized.shape[0]
        if verbose and nsub==1:
            print 'psf_size ', psf_size
        if display:
            # write this psf to fits
            pyfits.writeto('psf_resized_'+imtype+'_sub'+str(nsub)+'.fits',
                           psf_ima_resized, clobber=True)
            #result = show_image(psf_ima_resized)
        if psf_size % 2 != 0:
            print 'WARNING: PSF image not even in both dimensions!'

        # normalize to unity
        psf_ima_resized_norm = psf_ima_resized / np.sum(psf_ima_resized)
            
        # now place this resized and normalized PSF image at the
        # center of an image with the same size as the fftimage
        if ysize_fft % 2 != 0 or xsize_fft % 2 != 0:
            print 'WARNING: image not even in both dimensions!'
            
        xcenter_fft, ycenter_fft = xsize_fft/2, ysize_fft/2
        if verbose and nsub==1:
            print 'xcenter_fft, ycenter_fft ', xcenter_fft, ycenter_fft
        psf_ima_center[nsub, ycenter_fft-psf_size/2:ycenter_fft+psf_size/2, 
                       xcenter_fft-psf_size/2:xcenter_fft+psf_size/2] = psf_ima_resized_norm

        if display:
            pyfits.writeto('psf_center_'+imtype+'_sub'+str(nsub)+'.fits',
                           psf_ima_center[nsub], clobber=True)            
            #result = show_image(psf_ima_center[nsub])

        # perform fft shift
        psf_ima_shift[nsub] = fft.fftshift(psf_ima_center[nsub])
        #result = show_image(psf_ima_shift[nsub])

    if timing: print 'wall-time spent in get_psf', time.time() - t

    return psf_ima_shift, psf_ima_orig


def get_fratio_radec(psfcat_new, psfcat_ref, sexcat_new, sexcat_ref):

    """Function that takes in output catalogs of stars used in the PSFex
    runs on the new and the ref image, and returns the arrays x, y (in
    the new frame) and fratios for the matching stars. In addition, it
    provides the difference in stars' RAs and DECs in arcseconds
    between the two catalogs.

    """
    
    t = time.time()
    print '\nexecuting get_fratio_radec ...'
    
    def readcat (psfcat):
        table = ascii.read(psfcat, format='sextractor')
        number = table['SOURCE_NUMBER']
        x = table['X_IMAGE']
        y = table['Y_IMAGE']
        norm = table['NORM_PSF']
        return number, x, y, norm
        
    # read psfcat_new
    number_new, x_new, y_new, norm_new = readcat(psfcat_new)
    # read psfcat_ref
    number_ref, x_ref, y_ref, norm_ref = readcat(psfcat_ref)

    def xy2radec (number, sexcat):
        # read SExctractor fits table
        with pyfits.open(sexcat) as hdulist:
            data = hdulist[2].data
            ra_sex = data['ALPHAWIN_J2000']
            dec_sex = data['DELTAWIN_J2000']
        # loop numbers and record in ra, dec
        ra = []
        dec = []
        for n in number:
            ra.append(ra_sex[n-1])
            dec.append(dec_sex[n-1])      
        return np.array(ra), np.array(dec)
    
    # get ra, dec corresponding to x, y
    ra_new, dec_new = xy2radec(number_new, sexcat_new)
    ra_ref, dec_ref = xy2radec(number_ref, sexcat_ref)

    # now find matching entries
    x_new_match = []
    y_new_match = []
    dra_match = []
    ddec_match = []
    fratio = []
    nmatch = 0
    for i_new in range(len(x_new)):
        # calculate distance to ref objects
        dra = 3600.*(ra_new[i_new]-ra_ref)*np.cos(dec_ref[i_new]*np.pi/180.)
        ddec = 3600.*(dec_new[i_new]-dec_ref)
        dist = np.sqrt(dra**2 + ddec**2)
        # minimum distance and its index
        dist_min, i_ref = np.amin(dist), np.argmin(dist)
        if dist_min < 1.:
            nmatch += 1
            # append ratio of normalized counts to fratios
            x_new_match.append(x_new[i_new])
            y_new_match.append(y_new[i_new])
            dra_match.append(dra[i_ref])
            ddec_match.append(ddec[i_ref])
            fratio.append(norm_new[i_new] / norm_ref[i_ref])

    if verbose:
        print 'fraction of PSF stars that match', float(nmatch)/len(x_new)
            
    if timing: print 'wall-time spent in get_fratio_radec', time.time()-t

    return np.array(x_new_match), np.array(y_new_match), np.array(fratio), \
        np.array(dra_match), np.array(ddec_match)


def centers_cutouts(subsize, ysize, xsize):

    """Function that determines the input image indices (!) of the centers
    (list of nsubs x 2 elements) and cut-out regions (list of nsubs x
    4 elements) of image with the size xsize x ysize. Subsize is the
    fixed size of the subimages, e.g. 512 or 1024. The routine will
    fit as many of these in the full frames, and will calculate the
    remaining subimages.

    """

    nxsubs = xsize / subsize
    nysubs = ysize / subsize
    if xsize % subsize != 0 and ysize % subsize != 0:
        nxsubs += 1
        nysubs += 1
        remainder = True
    else:
        remainder = False
    nsubs = nxsubs * nysubs
    print 'nxsubs, nysubs, nsubs', nxsubs, nysubs, nsubs

    centers = np.ndarray((nsubs, 2), dtype=int)
    cuts_ima = np.ndarray((nsubs, 4), dtype=int)
    cuts_ima_fft = np.ndarray((nsubs, 4), dtype=int)
    cuts_fft = np.ndarray((nsubs, 4), dtype=int)
    sizes = np.ndarray((nsubs, 2), dtype=int)

    ysize_fft = subsize + 2*subimage_border
    xsize_fft = subsize + 2*subimage_border
        
    nsub = -1
    for i in range(nxsubs): 
        nx = subsize
        if i == nxsubs-1 and remainder:
            nx = xsize % subsize
        for j in range(nysubs):
            ny = subsize
            if j == nysubs-1 and remainder:
                ny = ysize % subsize
            x = i*subsize + nx/2
            y = j*subsize + ny/2
            nsub += 1
            centers[nsub] = [y, x]
            cuts_ima[nsub] = [y-ny/2, y+ny/2, x-nx/2, x+nx/2]
            y1 = np.amax([0,y-ny/2-subimage_border])
            x1 = np.amax([0,x-nx/2-subimage_border])
            y2 = np.amin([ysize,y+ny/2+subimage_border])
            x2 = np.amin([xsize,x+nx/2+subimage_border])
            cuts_ima_fft[nsub] = [y1,y2,x1,x2]
            cuts_fft[nsub] = [y1-(y-ny/2-subimage_border),ysize_fft-(y+ny/2+subimage_border-y2),
                              x1-(x-nx/2-subimage_border),xsize_fft-(x+nx/2+subimage_border-x2)]
            sizes[nsub] = [ny, nx]
            
    return centers, cuts_ima, cuts_ima_fft, cuts_fft, sizes


def show_image(image):

    im = plt.imshow(np.real(image), origin='lower', cmap='gist_heat',
                    interpolation='nearest')
    plt.show(im)


def run_wcs(image_in, image_out, ra, dec, gain, readnoise):

    if timing: t = time.time()
    print '\nexecuting run_wcs ...'
    
    scale_low = 0.95 * pixelscale
    scale_high = 1.05 * pixelscale

    cmd_old = ['solve-field', '--no-plots', '--no-fits2fits',
               image_in,
               '--tweak-order', str(3), '--scale-low', str(scale_low),
               '--scale-high', str(scale_high), '--scale-units', 'app',
               '--ra', str(ra), '--dec', str(dec), '--radius', str(2.),
               '--new-fits', image_out, '--overwrite']

    sexcat = image_out.replace('.fits','.sexcat')
    
    #scampcat = image_in.replace('.fits','.scamp')
    cmd = ['solve-field', '--no-plots', '--no-fits2fits',
           '--sextractor-config', 'Config/sex.config',
           '--x-column', 'XWIN_IMAGE', '--y-column', 'YWIN_IMAGE',
           '--sort-column', 'FLUX_AUTO',
           '--no-remove-lines',
           '--keep-xylist', sexcat,
           #'--scamp', scampcat,
           image_in,
           '--tweak-order', str(3), '--scale-low', str(scale_low),
           '--scale-high', str(scale_high), '--scale-units', 'app',
           '--ra', str(ra), '--dec', str(dec), '--radius', str(2.),
           '--new-fits', image_out, '--overwrite']

    if background_sex:
        bkg = image_in.replace('.fits','_bkg.fits')
        obj = image_in.replace('.fits','_obj.fits')
        cmd += ['--sextractor-path',
                'sex -CHECKIMAGE_TYPE BACKGROUND,OBJECTS -CHECKIMAGE_NAME '+bkg+','+obj]
        
    
    result = call(cmd)

    if timing: t2 = time.time()

    # this is the file containing just the WCS solution from Astrometry.net
    wcsfile = image_in.replace('.fits', '.wcs')

    use_wcs_xy2rd = False
    if use_wcs_xy2rd:
        # run Astrometry.net's wcs-xy2rd on the unix command line to
        # convert XWIN_IMAGE and YWIN_IMAGE to RA and DEC (saved in a
        # two-column fits table [radecfile]) from the [sexcat] and
        # .wcs output files created by Astrometry.net
        radecfile = image_in.replace('.fits', '.radec')
        cmd = ['wcs-xy2rd', '-w', wcsfile, '-i', sexcat, '-o', radecfile,
               '-X', 'XWIN_IMAGE', '-Y', 'YWIN_IMAGE']
        result = call(cmd)
        # read file with new ra and dec
        with pyfits.open(radecfile) as hdulist:
            data_newradec = hdulist[1].data
        newra = data_newradec['RA']
        newdec = data_newradec['DEC']

    # convert SIP header keywords from Astrometry.net to PV keywords
    # that swarp, scamp (and sextractor) understand using this module
    # from David Shupe:
    sip_to_pv(image_out, image_out, tpv_format=False)

    # read data from SExtractor catalog produced in Astrometry.net
    with pyfits.open(sexcat) as hdulist:
        data_sexcat = hdulist[1].data

    if not use_wcs_xy2rd:
        # instead of wcs-xy2rd, use astropy.WCS to find RA, DEC
        # corresponding to XWIN_IMAGE, YWIN_IMAGE, based on WCS info
        # saved by Astrometry.net in .wcs file (wcsfile). The 3rd
        # parameter to wcs.all_pix2world indicates the pixel
        # coordinate of the frame origin. This avoids having to save
        # the new RAs and DECs to file and read them back into python
        # arrays. Although it gives a command line warning, it
        # provides the same RA and DEC as wcs-xy2rd and also as
        # SExtractor run independently on the WCS-ed image (i.e.  the
        # image_out in this function). The warning is the mismatch
        # between NAXES in the .wcs image (0) and that expected
        # by the routine (2).       
        wcs = WCS(wcsfile)
        newra, newdec = wcs.all_pix2world(data_sexcat['XWIN_IMAGE'],
                                          data_sexcat['YWIN_IMAGE'],
                                          1)

    # read header of WCS image produced by Astrometry.net to be put in
    # data part of the LDAC_IMHEAD extension of the LDAC fits table
    # below
    with pyfits.open(image_out) as hdulist:
        header_wcsimage = hdulist[0].header

    # add header of .axy extension as the SExtractor keywords are there,
    # although PSFex only seems to use 2 of them: SEXGAIN and SEXBKDEV.
    # Astrometry.net does not provide these values (zeros), so their
    # values need to be set.
    axycat = image_in.replace('.fits','.axy')
    with pyfits.open(axycat) as hdulist:
        header_axycat = hdulist[0].header
    header_axycat['FITSFILE'] = image_out
    header_axycat['SEXGAIN'] = gain
    # estimate background r.m.s. (needed by PSFex) from BACKGROUND column in sexcat
    header_axycat['SEXBKDEV'] = np.sqrt(np.median(data_sexcat['BACKGROUND'])
                                        * gain + readnoise) / gain
    print 'background r.m.s. estimate:', np.sqrt(np.median(data_sexcat['BACKGROUND'])
                                                 * gain + readnoise)/gain
        
    # replace old ra and dec with new ones
    data_sexcat['ALPHAWIN_J2000'] = newra
    data_sexcat['DELTAWIN_J2000'] = newdec

    # convert FITS table to LDAC format needed by PSFex
    result = fits2ldac(header_wcsimage+header_axycat,
                       data_sexcat, sexcat, doSort=True)
    
    if timing:
        print 'extra time for creating LDAC fits table', time.time()-t2
        print 'wall-time spent in run_wcs', time.time()-t


def fits2ldac (header4ext2, data4ext3, fits_ldac_out, doSort=True):

    """This function converts the binary FITS table from Astrometry.net to
    a binary FITS_LDAC table that can be read by PSFex. [header4ext2]
    is what will be recorded as a single long string in the data part
    of the 2nd extension of the output table [fits_ldac_out], and
    [data4ext3] is the data part of an HDU that will define both the
    header and data parts of extension 3 of [fits_ldac_out].

    """

    # convert header to single (very) long string
    ext2_str = header4ext2.tostring(endcard=False, padding=False)

    # if the following line is not added, the very end of the data
    # part of extension 2 is written to a fits table such that PSFex
    # runs into a segmentation fault when attempting to read it (took
    # me ages to find out!).
    ext2_str += 'END                                                                          END'

    # read into string array
    ext2_data = np.array([ext2_str])

    # determine format string for header of extention 2
    formatstr = str(len(ext2_str))+'A'
    # create table 1
    col1 = pyfits.Column(name='Field Header Card', array=ext2_data, format=formatstr)
    cols = pyfits.ColDefs([col1])
    ext2 = pyfits.BinTableHDU.from_columns(cols)
    # make sure these keywords are in the header
    ext2.header['EXTNAME'] = 'LDAC_IMHEAD'
    ext2.header['TDIM1'] = '(80, {0})'.format(len(ext2_str)/80)

    # simply create extension 3 from [data4ext3]
    ext3 = pyfits.BinTableHDU(data4ext3)
    # extname needs to be as follows
    ext3.header['EXTNAME'] = 'LDAC_OBJECTS'

    # sort output table by number column if needed
    if doSort:
        index_sort = np.argsort(ext3.data['NUMBER'])
        ext3.data = ext3.data[index_sort]
    
    # create primary HDU
    prihdr = pyfits.Header()
    prihdu = pyfits.PrimaryHDU(header=prihdr)
    
    # write hdulist to output LDAC fits table
    hdulist = pyfits.HDUList([prihdu, ext2, ext3])
    hdulist.writeto(fits_ldac_out, clobber=True)
    hdulist.close()
    
    
def run_remap(image_new, image_ref, image_out,
              image_out_size, gain, config='Config/swarp.config'):
        
    """Function that remaps [image_ref] onto the coordinate grid of
       [image_new] and saves the resulting image in [image_out] with
       size [image_size].
    """

    if timing: t = time.time()
    print '\nexecuting run_remap ...'

    # read headers
    t = time.time()
    with pyfits.open(image_new) as hdulist:
        header_new = hdulist[0].header
    with pyfits.open(image_ref) as hdulist:
        header_ref = hdulist[0].header
        
    # create .head file with header info from [image_new]
    header_out = header_new[:]
    # copy some keywords from header_ref
    for key in ['EXPTIME','SATURATE','GAIN','RDNOISE','SEEING']:
        header_out[key] = header_ref[key]
    # delete some others
    for key in ['WCSAXES','NAXIS1', 'NAXIS2']:
        del header_out[key]
    # write to .head file
    with open(image_out.replace('.fits','.head'),'w') as newrefhdr:
        for card in header_out.cards:
            newrefhdr.write(str(card)+'\n')

    size_str = str(image_out_size[1]) + ',' + str(image_out_size[0]) 
    cmd = ['swarp', image_ref, '-c', config, '-IMAGEOUT_NAME', image_out, 
           '-IMAGE_SIZE', size_str, '-GAIN_DEFAULT', str(gain)]
    result = call(cmd)
    
    if timing: print 'wall-time spent in run_remap', time.time()-t

def run_sextractor(image, cat_out, file_config, file_catparams, fitpsf=False):

    """Function that runs SExtractor on [image], and saves the output
       catalog in [outcat], using the configuration file [file_config]
       and the parameters defining the output recorded in the
       catalogue [file_catparams]."""

    if timing: t = time.time()
    print '\nexecuting run_sextractor ...'

    # run sextractor from the unix command line
    cmd = ['sex', image, '-c', file_config, '-CATALOG_NAME', cat_out, 
           '-PARAMETERS_NAME', file_catparams]
    if fitpsf:
        # provide PSF file from PSFex
        cmd += ['-PSF_NAME', image.replace('.fits', '.psf')]

    result = call(cmd)

    if timing: print 'wall-time spent in run_sextractor', time.time()-t

def run_psfex(cat_in, file_config, cat_out):
    
    """Function that runs PSFEx on [cat_in] (which is a SExtractor output
       catalog in FITS_LDAC format) using the configuration file
       [file_config]"""

    if timing: t = time.time()

    # run psfex from the unix command line
    cmd = ['psfex', cat_in, '-c', file_config,'-OUTCAT_NAME', cat_out]
    result = call(cmd)    

    if timing: print 'wall-time spent in run_psfex', time.time()-t
    
# edited Barak's original code to include variances sigma_n**2 and
# sigma_r**2 (see Eq. 9, here sn and sr) and Fn and Fr which are
# assumed to be unity in Barak's code.
def run_ZOGY(R,N,Pr,Pn,sr,sn,fr,fn,Vr,Vn,dx,dy):
    
    if timing: t = time.time()

    R_hat = fft.fft2(R)
    N_hat = fft.fft2(N)
    Pn_hat = fft.fft2(Pn)
    Pn_hat2_abs = abs(Pn_hat**2)
    Pr_hat = fft.fft2(Pr)
    Pr_hat2_abs = abs(Pr_hat**2)

    sn2 = sn**2
    sr2 = sr**2
    #beta = fn/fr
    #beta2 = beta**2
    fn2 = fn**2
    fr2 = fr**2
    fD = fr*fn / np.sqrt(sn2*fr2+sr2*fn2)

    denominator = sn2*fr2*Pr_hat2_abs + sr2*fn2*Pn_hat2_abs
    #denominator_beta = sn2*Pr_hat2_abs + beta2*sr2*Pn_hat2_abs

    D_hat = (fr*Pr_hat*N_hat - fn*Pn_hat*R_hat) / np.sqrt(denominator)
    # alternatively using beta:
    #D_hat = (Pr_hat*N_hat - beta*Pn_hat*R_hat) / np.sqrt(denominator_beta)

    D = np.real(fft.ifft2(D_hat)) / fD
    
    P_D_hat = (fr*fn/fD) * (Pr_hat*Pn_hat) / np.sqrt(denominator)
    #alternatively using beta:
    #P_D_hat = np.sqrt(sn2+beta2*sr2)*(Pr_hat*Pn_hat) / np.sqrt(denominator_beta)

    #P_D = np.real(fft.ifft2(P_D_hat))

    S_hat = fD*D_hat*np.conj(P_D_hat)
    S = np.real(fft.ifft2(S_hat))

    # PMV 2017/01/18: added following part based on Eqs. 25-31
    # from Barak's paper
    kr_hat = fr*fn2*np.conj(Pr_hat)*Pn_hat2_abs / denominator
    kr = np.real(fft.ifft2(kr_hat))
    kr2 = kr**2
    kr2_hat = fft.fft2(kr2)

    kn_hat = fn*fr2*np.conj(Pn_hat)*Pr_hat2_abs / denominator
    kn = np.real(fft.ifft2(kn_hat))
    kn2 = kn**2
    kn2_hat = fft.fft2(kn2)
    
    Vr_hat = fft.fft2(Vr)
    Vn_hat = fft.fft2(Vn)

    VSr = np.real(fft.ifft2(Vr_hat*kr2_hat))
    VSn = np.real(fft.ifft2(Vn_hat*kn2_hat))

    dx2 = dx**2
    dy2 = dy**2
    # and calculate astrometric variance
    Sn = np.real(fft.ifft2(kn_hat*N_hat))
    dSndy = Sn - np.roll(Sn,1,axis=1)
    dSndx = Sn - np.roll(Sn,1,axis=0)
    VSn_ast = dx2 * dSndx**2 + dy2 * dSndy**2
    
    Sr = np.real(fft.ifft2(kr_hat*R_hat))
    dSrdy = Sr - np.roll(Sr,1,axis=1)
    dSrdx = Sr - np.roll(Sr,1,axis=0)
    VSr_ast = dx2 * dSrdx**2 + dy2 * dSrdy**2

    if verbose:
        print 'fD', fD
        #print 'kr_hat is finite?', np.all(np.isfinite(kr_hat))
        #print 'kn_hat is finite?', np.all(np.isfinite(kn_hat))
        #print 'dSrdx is finite?', np.all(np.isfinite(dSrdx))
        #print 'dSrdy is finite?', np.all(np.isfinite(dSrdy))
        #print 'dSndy is finite?', np.all(np.isfinite(dSndy))
        #print 'dSndx is finite?', np.all(np.isfinite(dSndx))
        #print 'VSr_ast is finite?', np.all(np.isfinite(VSr_ast))
        #print 'VSn_ast is finite?', np.all(np.isfinite(VSn_ast))
        #print 'dx is finite?', np.isfinite(dx)
        #print 'dy is finite?', np.isfinite(dy)
    
    if display:
        pyfits.writeto('kr.fits', np.real(kr), clobber=True)
        pyfits.writeto('kn.fits', np.real(kn), clobber=True)
        pyfits.writeto('Sr.fits', Sr, clobber=True)
        pyfits.writeto('Sn.fits', Sn, clobber=True)
        pyfits.writeto('VSr.fits', VSr, clobber=True)
        pyfits.writeto('VSn.fits', VSn, clobber=True)
        pyfits.writeto('VSr_ast.fits', VSr_ast, clobber=True)
        pyfits.writeto('VSn_ast.fits', VSn_ast, clobber=True)

    # and finally S_corr
    V = VSr + VSn + VSr_ast + VSn_ast
    #S_corr = S / np.sqrt(V)
    # make sure there's no division by zero
    S_corr = np.copy(S)
    S_corr[V>0] /= np.sqrt(V[V>0])
    
    if timing: print 'wall-time spent in optimal subtraction', time.time()-t

    return D, S, S_corr


# original code from Barak (this assumes fr and fn are unity, and it
# does not calculate the variance images needed for Scorr):
def optimal_binary_image_subtraction(R,N,Pr,Pn,sr,sn):
    R_hat = fft.fft2(R)
    N_hat = fft.fft2(N)
    Pn_hat = fft.fft2(Pn)
    Pr_hat = fft.fft2(Pr)
    G_hat = (Pr_hat*N_hat - Pn_hat*R_hat) / np.sqrt((sr**2*abs(Pn_hat**2) + sn**2*abs(Pr_hat**2)))
    P_G_hat = (Pr_hat*Pn_hat) / np.sqrt((sr**2*abs(Pn_hat**2) + sn**2*abs(Pr_hat**2)))
    S_hat = G_hat*conj(P_G_hat)
    #S_hat = (conj(Pn_hat)*np.abs(Pr_hat)**2*N_hat - conj(Pr_hat)*np.abs(Pn_hat)**2*R_hat) / (sr**2*abs(Pn_hat**2) + sn**2*abs(Pr_hat**2))
    S = fft.ifft2(S_hat)
    G = fft.ifft2(G_hat)
    P_G = real(fft.ifft2(P_G_hat))
    return S/std(S[15::30,15::30]), G/std(G[15::30,15::30]), P_G / sum(P_G)


def main():
    """Wrapper allowing optimal_subtraction to be run from the command line"""
    
    parser = argparse.ArgumentParser(description='Run optimal_subtraction on images')
    parser.add_argument('new_fits', help='filename of new image')
    parser.add_argument('ref_fits', help='filename of ref image')
    args = parser.parse_args()
    optimal_subtraction(args.new_fits, args.ref_fits)
        
if __name__ == "__main__":
    main()
