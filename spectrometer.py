import json
import picamera
import threading
import math, sys
from io import BytesIO
from time import sleep
from fractions import Fraction
from collections import OrderedDict
from PIL import Image, ImageDraw, ImageFile, ImageFont

import firebase_admin
from firebase_admin import credentials, db

cred = credentials.Certificate("spectrometer-bba6d-firebase-adminsdk-7c4pi-1daece715f.json")
firebase_admin.initialize_app(cred, {
    'databaseURL': 'https://spectrometer-bba6d.firebaseio.com'
})
ref = db.reference('')

#scan a column to determine top and bottom of area of lightness
def getSpectrumYBound(pix,x,middleY,spectrum_threshold,spectrum_threshold_duration):
	c=0
	spectrum_top=middleY
	for y in range(middleY,0,-1):
		r, g, b = pix[x,y]
		brightness=r+g+b
		if brightness<spectrum_threshold:		
			c=c+1
			if c>spectrum_threshold_duration:
				break;
		else:
			spectrum_top=y
			c=0

	c=0
	spectrum_bottom=middleY
	for y in range(middleY,middleY*2,1):
		r, g, b = pix[x,y]
		brightness=r+g+b
		if brightness<spectrum_threshold:
			c=c+1
			if c>spectrum_threshold_duration:
				break;
		else:
			spectrum_bottom=y
			c=0

	return spectrum_top,spectrum_bottom


#find aperture on right hand side of image along middle line
def findAperture(pix,width,middleX,middleY):
	aperture_brightest=0
	aperture_x=0
	for x in range(middleX,width,1):
		r, g, b = pix[x,middleY]
		brightness=r+g+b
		if brightness>aperture_brightest:
			aperture_brightest=brightness
			aperture_x=x
	
	aperture_threshold=aperture_brightest*0.9
	aperture_x1=aperture_x
	for x in range(aperture_x,middleX,-1):
		r, g, b = pix[x,middleY]
		brightness=r+g+b
		if brightness<aperture_threshold:
			aperture_x1=x
			break
	
	aperture_x2=aperture_x
	for x in range(aperture_x,width,1):
		r, g, b = pix[x,middleY]
		brightness=r+g+b
		if brightness<aperture_threshold:
			aperture_x2=x
			break
			
	aperture_x=(aperture_x1+aperture_x2)/2
	
	spectrum_threshold_duration=64
	apertureYBounds=getSpectrumYBound(pix,aperture_x,middleY,aperture_threshold,spectrum_threshold_duration);
	aperture_y=(apertureYBounds[0]+apertureYBounds[1])/2
	aperture_height=(apertureYBounds[1]-apertureYBounds[0])*0.9

	return { 'x':aperture_x, 'y':aperture_y, 'h':aperture_height, 'b': aperture_brightest }

# draw aperture onto image
def drawAperture(aperture,draw):
	draw.line((aperture['x'],aperture['y']-aperture['h']/2,aperture['x'],aperture['y']+aperture['h']/2),fill="#000")

#draw scan line
def drawScanLine(aperture,spectrumAngle,draw):
	xd=aperture['x']
	h=aperture['h']/2
	y0=math.tan(spectrumAngle)*xd+aperture['y']
	draw.line((0,y0-h,aperture['x'],aperture['y']-h),fill="#888")
	draw.line((0,y0+h,aperture['x'],aperture['y']+h),fill="#888")


#return an RGB visual representation of wavelength for chart
def wavelengthToColor(lambda2):
    # Based on: http://www.efg2.com/Lab/ScienceAndEngineering/Spectra.htm
    # The foregoing is based on: http://www.midnightkite.com/color.html
    factor = 0.0;

    color=[0,0,0]
    #thresholds = [ 380, 440, 490, 510, 580, 645, 780 ];
    #                    vio  blu  cyn  gre  yel  end       
    thresholds =  [ 380, 400, 450, 465, 520, 565, 780 ];
    for i in range(0,len(thresholds)-1,1):
        t1 = thresholds[i]
        t2 = thresholds[i+1]
        if (lambda2 < t1 or lambda2 >= t2):
        	continue
        if (i%2!=0):
        	tmp=t1
        	t1=t2
        	t2=tmp
        if i<5:
        	color[ i % 3] = (lambda2 - t2) / (t1-t2)
        color[ 2-i/2] = 1.0;
        factor = 1.0;
        break

	
    #Let the intensity fall off near the vision limits
    if (lambda2 >= 380 and lambda2 < 420):
        factor = 0.2 + 0.8*(lambda2-380) / (420 - 380);
    elif (lambda2 >= 600 and lambda2 < 780):
        factor = 0.2 + 0.8*(780 - lambda2) / (780 - 600);
    return ( int(255*color[0]*factor),int(255*color[1]*factor),int(255*color[2]*factor) )

class ImageProcessor(threading.Thread):
    def __init__(self, owner):
        super(ImageProcessor, self).__init__()
        self.stream = BytesIO()
        self.event = threading.Event()
        self.terminated = False
        self.owner = owner
        self.start()

    def run(self):
        # This method runs in a separate thread
        while not self.terminated:
            # Wait for an image to be written to the stream
            if self.event.wait(1):
                try:
                    self.stream.seek(0)
                    # Read the image and do some processing on it
                    print "Processing image"
                    processImage(self)
                finally:
                    # Reset the stream and event
                    self.stream.seek(0)
                    self.stream.truncate()
                    self.event.clear()
                    # Return ourselves to the available pool
                    with self.owner.lock:
                        self.owner.pool.append(self)

class ProcessOutput(object):
    def __init__(self):
        self.done = False
        # Construct a pool of 4 image processors along with a lock
        # to control access between threads
        self.lock = threading.Lock()
        self.pool = [ImageProcessor(self) for i in range(4)]
        self.processor = None

    def write(self, buf):
        if buf.startswith(b'\xff\xd8'):
            # New frame; set the current processor going and grab
            # a spare one
            if self.processor:
                self.processor.event.set()
            with self.lock:
                if self.pool:
                    self.processor = self.pool.pop()
                else:
                    # No processor's available, we'll have to skip
                    print "Skipping frame"
                    self.processor = None
        if self.processor:
            self.processor.stream.write(buf)

    def flush(self):
        # When told to flush (this indicates end of recording), shut
        # down in an orderly fashion. First, add the current processor
        # back to the pool
        if self.processor:
            with self.lock:
                self.pool.append(self.processor)
                self.processor = None
        # Now, empty the pool, joining each thread as we go
        while True:
            with self.lock:
                try:
                    proc = self.pool.pop()
                except IndexError:
                    pass # pool is empty
            proc.terminated = True
            proc.join()

def processImage(self):
    name = sys.argv[1]
    im = Image.open(self.stream)
    width=im.size[0]
    height=im.size[1]
    middleY=height/2
    middleX=width/2
    pix = im.load()
    #print im.bits, im.size, im.format

    draw=ImageDraw.Draw(im)
    spectrumAngle=0.03

    aperture=findAperture(pix,width,middleX,middleY)
    #print aperture
    drawAperture(aperture,draw);
    drawScanLine(aperture,spectrumAngle,draw)


    wavelengthFactor=0.892 # 1000/mm
    #wavelengthFactor=0.892*2.0*600/650 # 500/mm

    xd=aperture['x']
    h=aperture['h']/2
    step=1
    last_graphY=0
    maxResult=0
    results=OrderedDict()
    for x in range(0,xd*7/8,step):
        wavelength=(xd-x)*wavelengthFactor
        if (wavelength<380):
    		continue
    	if (wavelength>1000):
    		continue
    	
    	#general efficiency curve of 1000/mm grating
    	eff=(800-(wavelength-250))/800
    	if (eff<0.3):
    		eff=0.3
    		
    	
    	#notch near yellow maybe caused by camera sensitivity
    	mid=575
    	width=10
    	if (wavelength>(mid-width) and wavelength<(mid+width)):
    		d=(width-abs(wavelength-mid))/width
    		eff=eff*(1-d*0.1);

    	#up notch near 590
    	mid=588
    	width=10
    	if (wavelength>(mid-width) and wavelength<(mid+width)):
    		d=(width-abs(wavelength-mid))/width
    		eff=eff*(1+d*0.1);


    	
    	y0=math.tan(spectrumAngle)*(xd-x)+aperture['y']
    	amplitude=0
    	ac=0.0
    	for y in range(int(y0-h),int(y0+h),1):
    		r, g, b = pix[x,y]
    		#q=math.sqrt(r*r+b*b+g*g*1.5);
    		q=r+b+g*2
    		if y<(y0-h+2) or y>(y0+h-3):
    			q=q*0.5
    		amplitude=amplitude+q
    		ac=ac+1.0
    	amplitude=amplitude/(ac)/(eff)
    	#amplitude=1/eff
    	results[str(wavelength)]=amplitude
    	if amplitude>maxResult:
    		maxResult=amplitude
    	graphY=amplitude/50*h
    	draw.line((x-step,y0+h-last_graphY, x,y0+h-graphY),fill="#fff")
    	last_graphY=graphY

    for wl in range(400,1001,50):
    	x=xd-(wl/wavelengthFactor)
    	y0=math.tan(spectrumAngle)*(xd-x)+aperture['y']
    	draw.line((x,y0+h+5, x,y0+h-5))
    	draw.text((x,y0+h+15),str(wl))
    	
    exposure=maxResult/(255+255+255)
    print "ideal exposure between 0.15 and 0.30"
    print "exposure=",exposure
    if (exposure<0.15):
    	print "consider increasing shutter time"
    elif (exposure>0.3):
    	print "consider reducing shutter time"

    #normalise results
    for wavelength in results:
    	results[wavelength]=results[wavelength]/maxResult
	
    dataPoints = []
    for wavelength in results:
	dataPoints.append({
	    'wavelength': wavelength,
	    'amplitude': "{:0.3f}".format(results[wavelength])
	})
    ref.update({'graph': dataPoints})
    print "Firebase updated"
    

with picamera.PiCamera() as camera:
    camera.vflip = True
    camera.framerate = Fraction(1, 6)
    camera.sensor_mode = 3
    camera.shutter_speed = long(sys.argv[2])
    camera.iso=800
    camera.exposure_mode = 'off'
    camera.awb_mode='off'
    camera.awb_gains=(1,1)
    camera.start_preview()
    sleep(2)
    output = ProcessOutput()
    camera.start_recording(output, format='mjpeg')
    while not output.done:
        camera.wait_recording(1)
    camera.stop_recording()
