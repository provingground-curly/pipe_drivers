from __future__ import absolute_import, division, print_function

import lsst.afw.math as afwMath
import lsst.afw.image as afwImage

from lsst.pipe.base import ArgumentParser, Struct
from lsst.pex.config import Config, Field, ConfigurableField, ConfigField
from lsst.ctrl.pool.pool import Pool
from lsst.ctrl.pool.parallel import BatchPoolTask
from lsst.pipe.drivers.background import (SkyMeasurementTask, FocalPlaneBackground,
                                          FocalPlaneBackgroundConfig, MaskObjectsTask)
import lsst.pipe.drivers.visualizeVisit as visualizeVisit

DEBUG = False  # Debugging outputs?


def makeCameraImage(camera, exposures, filename=None, binning=8):
    """Make and write an image of an entire focal plane

    Parameters
    ----------
    camera : `lsst.afw.cameraGeom.Camera`
        Camera description.
    exposures : `list` of `tuple` of `int` and `lsst.afw.image.Exposure`
        List of detector ID and CCD exposures (binned by `binning`).
    filename : `str`, optional
        Output filename.
    binning : `int`
        Binning size that has been applied to images.
    """
    image = visualizeVisit.makeCameraImage(camera, dict(exp for exp in exposures if exp is not None), binning)
    if filename is not None:
        image.writeFits(filename)
    return image


class SkyCorrectionConfig(Config):
    """Configuration for SkyCorrectionTask"""
    bgModel = ConfigField(dtype=FocalPlaneBackgroundConfig, doc="Background model")
    bgModel2 = ConfigField(dtype=FocalPlaneBackgroundConfig, doc="2nd Background model")
    sky = ConfigurableField(target=SkyMeasurementTask, doc="Sky measurement")
    maskObjects = ConfigurableField(target=MaskObjectsTask, doc="Mask Objects")
    doMaskObjects = Field(dtype=bool, default=True, doc="Mask objects to find good sky?")
    doBgModel = Field(dtype=bool, default=True, doc="Do background model subtraction?")
    doBgModel2 = Field(dtype=bool, default=True, doc="Do cleanup background model subtraction?")
    doSky = Field(dtype=bool, default=True, doc="Do sky frame subtraction?")
    binning = Field(dtype=int, default=8, doc="Binning factor for constructing focal-plane images")
    hasFakes = Field(dtype=bool, default=False,
                     doc="Should be set to True if fake sources were added to the data before processing.")

    def setDefaults(self):
        Config.setDefaults(self)
        self.bgModel2.doSmooth = True
        self.bgModel2.minFrac = 0.5
        self.bgModel2.xSize = 256
        self.bgModel2.ySize = 256
        self.bgModel2.smoothScale = 1.0


class SkyCorrectionTask(BatchPoolTask):
    """Correct sky over entire focal plane"""
    ConfigClass = SkyCorrectionConfig
    _DefaultName = "skyCorr"

    def __init__(self, *args, **kwargs):
        BatchPoolTask.__init__(self, *args, **kwargs)
        self.makeSubtask("maskObjects")
        self.makeSubtask("sky")

        if self.config.hasFakes:
            self.calexpType = "fakes_calexp"
        else:
            self.calexpType = "calexp"

    @classmethod
    def _makeArgumentParser(cls, *args, **kwargs):
        kwargs.pop("doBatch", False)
        parser = ArgumentParser(name="skyCorr", *args, **kwargs)
        parser.add_id_argument("--id", datasetType="calexp", level="visit",
                               help="data ID, e.g. --id visit=12345")
        return parser

    @classmethod
    def batchWallTime(cls, time, parsedCmd, numCores):
        """Return walltime request for batch job

        Subclasses should override if the walltime should be calculated
        differently (e.g., addition of some serial time).

        Parameters
        ----------
        time : `float`
            Requested time per iteration.
        parsedCmd : `argparse.Namespace`
            Results of argument parsing.
        numCores : `int`
            Number of cores.
        """
        numTargets = len(cls.RunnerClass.getTargetList(parsedCmd))
        return time*numTargets

    def runDataRef(self, expRef):
        """Perform sky correction on an exposure

        We restore the original sky, and remove it again using multiple
        algorithms. We optionally apply:

        1. A large-scale background model.
            This step removes very-large-scale sky such as moonlight.
        2. A sky frame.
        3. A medium-scale background model.
            This step removes residual sky (This is smooth on the focal plane).

        Only the master node executes this method. The data is held on
        the slave nodes, which do all the hard work.

        Parameters
        ----------
        expRef : `lsst.daf.persistence.ButlerDataRef`
            Data reference for exposure.
        """
        if DEBUG:
            extension = "-%(visit)d.fits" % expRef.dataId

        with self.logOperation("processing %s" % (expRef.dataId,)):
            pool = Pool()
            pool.cacheClear()
            pool.storeSet(butler=expRef.getButler())
            camera = expRef.get("camera")

            dataIdList = [ccdRef.dataId for ccdRef in expRef.subItems("ccd") if
                          ccdRef.datasetExists(self.calexpType)]

            exposures = pool.map(self.loadImage, dataIdList)
            if DEBUG:
                makeCameraImage(camera, exposures, "restored" + extension)
                exposures = pool.mapToPrevious(self.collectOriginal, dataIdList)
                makeCameraImage(camera, exposures, "original" + extension)
                exposures = pool.mapToPrevious(self.collectMask, dataIdList)
                makeCameraImage(camera, exposures, "mask" + extension)

            if self.config.doBgModel:
                exposures = self.focalPlaneBackground(camera, pool, dataIdList, self.config.bgModel)

            if self.config.doSky:
                measScales = pool.mapToPrevious(self.measureSkyFrame, dataIdList)
                scale = self.sky.solveScales(measScales)
                self.log.info("Sky frame scale: %s" % (scale,))
                exposures = pool.mapToPrevious(self.subtractSkyFrame, dataIdList, scale)
                if DEBUG:
                    makeCameraImage(camera, exposures, "skysub" + extension)
                    calibs = pool.mapToPrevious(self.collectSky, dataIdList)
                    makeCameraImage(camera, calibs, "sky" + extension)

            if self.config.doBgModel2:
                exposures = self.focalPlaneBackground(camera, pool, dataIdList, self.config.bgModel2)

            # Persist camera-level image of calexp
            image = makeCameraImage(camera, exposures)
            expRef.put(image, "calexp_camera")

            pool.mapToPrevious(self.write, dataIdList)

    def focalPlaneBackground(self, camera, pool, dataIdList, config):
        """Perform full focal-plane background subtraction

        This method runs on the master node.

        Parameters
        ----------
        camera : `lsst.afw.cameraGeom.Camera`
            Camera description.
        pool : `lsst.ctrl.pool.Pool`
            Process pool.
        dataIdList : iterable of `dict`
            List of data identifiers for the CCDs.
        config : `lsst.pipe.drivers.background.FocalPlaneBackgroundConfig`
            Configuration to use for background subtraction.

        Returns
        -------
        exposures : `list` of `lsst.afw.image.Image`
            List of binned images, for creating focal plane image.
        """
        bgModel = FocalPlaneBackground.fromCamera(config, camera)
        data = [Struct(dataId=dataId, bgModel=bgModel.clone()) for dataId in dataIdList]
        bgModelList = pool.mapToPrevious(self.accumulateModel, data)
        for ii, bg in enumerate(bgModelList):
            self.log.info("Background %d: %d pixels", ii, bg._numbers.array.sum())
            bgModel.merge(bg)
        return pool.mapToPrevious(self.subtractModel, dataIdList, bgModel)

    def loadImage(self, cache, dataId):
        """Load original image and restore the sky

        This method runs on the slave nodes.

        Parameters
        ----------
        cache : `lsst.pipe.base.Struct`
            Process pool cache.
        dataId : `dict`
            Data identifier.

        Returns
        -------
        exposure : `lsst.afw.image.Exposure`
            Resultant exposure.
        """
        cache.dataId = dataId
        cache.exposure = cache.butler.get(self.calexpType, dataId, immediate=True).clone()
        bgOld = cache.butler.get("calexpBackground", dataId, immediate=True)
        image = cache.exposure.getMaskedImage()

        # We're removing the old background, so change the sense of all its components
        for bgData in bgOld:
            statsImage = bgData[0].getStatsImage()
            statsImage *= -1

        image -= bgOld.getImage()
        cache.bgList = afwMath.BackgroundList()
        for bgData in bgOld:
            cache.bgList.append(bgData)

        if self.config.doMaskObjects:
            self.maskObjects.findObjects(cache.exposure)

        return self.collect(cache)

    def measureSkyFrame(self, cache, dataId):
        """Measure scale for sky frame

        This method runs on the slave nodes.

        Parameters
        ----------
        cache : `lsst.pipe.base.Struct`
            Process pool cache.
        dataId : `dict`
            Data identifier.

        Returns
        -------
        scale : `float`
            Scale for sky frame.
        """
        assert cache.dataId == dataId
        cache.sky = self.sky.getSkyData(cache.butler, dataId)
        scale = self.sky.measureScale(cache.exposure.getMaskedImage(), cache.sky)
        return scale

    def subtractSkyFrame(self, cache, dataId, scale):
        """Subtract sky frame

        This method runs on the slave nodes.

        Parameters
        ----------
        cache : `lsst.pipe.base.Struct`
            Process pool cache.
        dataId : `dict`
            Data identifier.
        scale : `float`
            Scale for sky frame.

        Returns
        -------
        exposure : `lsst.afw.image.Exposure`
            Resultant exposure.
        """
        assert cache.dataId == dataId
        self.sky.subtractSkyFrame(cache.exposure.getMaskedImage(), cache.sky, scale, cache.bgList)
        return self.collect(cache)

    def accumulateModel(self, cache, data):
        """Fit background model for CCD

        This method runs on the slave nodes.

        Parameters
        ----------
        cache : `lsst.pipe.base.Struct`
            Process pool cache.
        data : `lsst.pipe.base.Struct`
            Data identifier, with `dataId` (data identifier) and `bgModel`
            (background model) elements.

        Returns
        -------
        bgModel : `lsst.pipe.drivers.background.FocalPlaneBackground`
            Background model.
        """
        assert cache.dataId == data.dataId
        data.bgModel.addCcd(cache.exposure)
        return data.bgModel

    def subtractModel(self, cache, dataId, bgModel):
        """Subtract background model

        This method runs on the slave nodes.

        Parameters
        ----------
        cache : `lsst.pipe.base.Struct`
            Process pool cache.
        dataId : `dict`
            Data identifier.
        bgModel : `lsst.pipe.drivers.background.FocalPlaneBackround`
            Background model.

        Returns
        -------
        exposure : `lsst.afw.image.Exposure`
            Resultant exposure.
        """
        assert cache.dataId == dataId
        exposure = cache.exposure
        image = exposure.getMaskedImage()
        detector = exposure.getDetector()
        bbox = image.getBBox()
        cache.bgModel = bgModel.toCcdBackground(detector, bbox)
        image -= cache.bgModel.getImage()
        cache.bgList.append(cache.bgModel[0])
        return self.collect(cache)

    def realiseModel(self, cache, dataId, bgModel):
        """Generate an image of the background model for visualisation

        Useful for debugging.

        Parameters
        ----------
        cache : `lsst.pipe.base.Struct`
            Process pool cache.
        dataId : `dict`
            Data identifier.
        bgModel : `lsst.pipe.drivers.background.FocalPlaneBackround`
            Background model.

        Returns
        -------
        detId : `int`
            Detector identifier.
        image : `lsst.afw.image.MaskedImage`
            Binned background model image.
        """
        assert cache.dataId == dataId
        exposure = cache.exposure
        detector = exposure.getDetector()
        bbox = exposure.getMaskedImage().getBBox()
        image = bgModel.toCcdBackground(detector, bbox).getImage()
        return self.collectBinnedImage(exposure, image)

    def collectBinnedImage(self, exposure, image):
        """Return the binned image required for visualization

        This method just helps to cut down on boilerplate.

        Parameters
        ----------
        image : `lsst.afw.image.MaskedImage`
            Image to go into visualisation.

        Returns
        -------
        detId : `int`
            Detector identifier.
        image : `lsst.afw.image.MaskedImage`
            Binned image.
        """
        return (exposure.getDetector().getId(), afwMath.binImage(image, self.config.binning))

    def collect(self, cache):
        """Collect exposure for potential visualisation

        This method runs on the slave nodes.

        Parameters
        ----------
        cache : `lsst.pipe.base.Struct`
            Process pool cache.

        Returns
        -------
        detId : `int`
            Detector identifier.
        image : `lsst.afw.image.MaskedImage`
            Binned image.
        """
        return self.collectBinnedImage(cache.exposure, cache.exposure.maskedImage)

    def collectOriginal(self, cache, dataId):
        """Collect original image for visualisation

        This method runs on the slave nodes.

        Parameters
        ----------
        cache : `lsst.pipe.base.Struct`
            Process pool cache.
        dataId : `dict`
            Data identifier.

        Returns
        -------
        detId : `int`
            Detector identifier.
        image : `lsst.afw.image.MaskedImage`
            Binned image.
        """
        exposure = cache.butler.get("calexp", dataId, immediate=True)
        return self.collectBinnedImage(exposure, exposure.maskedImage)

    def collectSky(self, cache, dataId):
        """Collect original image for visualisation

        This method runs on the slave nodes.

        Parameters
        ----------
        cache : `lsst.pipe.base.Struct`
            Process pool cache.
        dataId : `dict`
            Data identifier.

        Returns
        -------
        detId : `int`
            Detector identifier.
        image : `lsst.afw.image.MaskedImage`
            Binned image.
        """
        return self.collectBinnedImage(cache.exposure, cache.sky.getImage())

    def collectMask(self, cache, dataId):
        """Collect mask for visualisation

        This method runs on the slave nodes.

        Parameters
        ----------
        cache : `lsst.pipe.base.Struct`
            Process pool cache.
        dataId : `dict`
            Data identifier.

        Returns
        -------
        detId : `int`
            Detector identifier.
        image : `lsst.afw.image.Image`
            Binned image.
        """
        # Convert Mask to floating-point image, because that's what's required for focal plane construction
        image = afwImage.ImageF(cache.exposure.maskedImage.getBBox())
        image.array[:] = cache.exposure.maskedImage.mask.array
        return self.collectBinnedImage(cache.exposure, image)

    def write(self, cache, dataId):
        """Write resultant background list

        This method runs on the slave nodes.

        Parameters
        ----------
        cache : `lsst.pipe.base.Struct`
            Process pool cache.
        dataId : `dict`
            Data identifier.
        """
        cache.butler.put(cache.bgList, "skyCorr", dataId)

    def _getMetadataName(self):
        """There's no metadata to write out"""
        return None
