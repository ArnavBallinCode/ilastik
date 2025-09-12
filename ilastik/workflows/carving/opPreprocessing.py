###############################################################################
#   ilastik: interactive learning and segmentation toolkit
#
#       Copyright (C) 2011-2024, the ilastik developers
#                                <team@ilastik.org>
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# In addition, as a special exception, the copyright holders of
# ilastik give you permission to combine ilastik with applets,
# workflows and plugins which are not covered under the GNU
# General Public License.
#
# See the LICENSE file for details. License information is also available
# on the ilastik web site at:
# 		   http://ilastik.org/license.html
###############################################################################
# Python
from builtins import range
from past.utils import old_div

# SciPy
import numpy
import vigra

# lazyflow
from lazyflow.roi import roiFromShape
from lazyflow.graph import Operator, InputSlot, OutputSlot
from lazyflow.operators import OpBlockedArrayCache

from lazyflow.request import Request

from lazyflow.utility.helpers import eq_shapes
from lazyflow.utility.timer import Timer
from ilastik.applets.base.applet import DatasetConstraintError

# carving backend in ilastiktools
from .watershed_segmentor import WatershedSegmentor

from .carvingTools import watershed_and_agglomerate, parallel_filter

import logging

logger = logging.getLogger(__name__)


class OpFilter(Operator):
    HESSIAN_BRIGHT = 0
    HESSIAN_DARK = 1
    STEP_EDGES = 2
    RAW = 3
    RAW_INVERTED = 4

    FILTER_NAMES = {
        0: "hessianOfGaussianEigenvalues",
        1: "hessianOfGaussianEigenvalues",
        2: "gaussianGradientMagnitude",
        3: "gaussianSmoothing",
        4: "gaussianSmoothing",
    }

    Input = InputSlot()
    Filter = InputSlot(value=HESSIAN_BRIGHT)
    Sigma = InputSlot(value=1.6)

    Overlay = InputSlot(optional=True)  # GUI-only.  Shown over raw data if available.

    Output = OutputSlot()

    def setupOutputs(self):
        self.Output.meta.assignFrom(self.Input.meta)
        self.Output.meta.dtype = numpy.float32

    def execute(self, slot, subindex, roi, result):
        # make sure raw data is 5D: t,{x,y,z},c
        ax = self.Input.meta.axistags
        sh = self.Input.meta.shape
        assert len(ax) == 5
        assert ax[0].key == "t" and sh[0] == 1
        for i in range(1, 4):
            assert ax[i].isSpatial()
        assert ax[4].key == "c" and sh[4] == 1

        volume5d = self.Input.value
        sigma = self.Sigma.value
        volume = volume5d[0, :, :, :, 0]
        result_view = result[0, :, :, :, 0]

        logger.info("input volume shape: %r" % (volume.shape,))
        logger.info("input volume size: %r MB", (old_div(volume.nbytes, 1024**2),))
        fvol = numpy.asarray(volume, numpy.float32)

        # Choose filter selected by user
        volume_filter = self.Filter.value
        filter_name = self.FILTER_NAMES[volume_filter]

        logger.info("applying filter on shape = %r" % (fvol.shape,))
        with Timer() as filterTimer:

            # check dimensionality of input and reduce to 2d volume
            # if we have actual 2d input
            if fvol.shape[2] == 1:
                fvol = fvol[:, :, 0]

            # we need to invert the input for filter mode RAW_INVERTED
            if volume_filter == OpFilter.RAW_INVERTED:
                fvol = -fvol

            # for the hessian filters, we only need to keep one channel,
            # and we discard the other channels during block-wise computation to save memory
            if volume_filter == OpFilter.HESSIAN_BRIGHT:  # HESSIAN_BRIGHT -> last eigenvalue
                channel = fvol.ndim - 1
            elif volume_filter == OpFilter.HESSIAN_DARK:  # HESSIAN_DARK -> first eigenvalue
                channel = 0
            else:
                channel = None

            # handle the special case of the Request threadpool not having any workers
            max_workers = max(1, Request.global_thread_pool.num_workers)
            # compute the filter response block-wise
            response = parallel_filter(filter_name, fvol, sigma, max_workers=max_workers, return_channel=channel)

            # need to invert response for hessian bright
            if volume_filter == OpFilter.HESSIAN_BRIGHT:
                response = numpy.max(response) - response

            # write the response to result view
            if fvol.ndim == 2:
                result_view[:, :, 0] = response
            else:
                result_view[...] = response

            logger.info("Filter took {} seconds".format(filterTimer.seconds()))

        return result

    def propagateDirty(self, slot, subindex, roi):
        self.Output.setDirty(slice(None))


class OpNormalize255(Operator):
    Input = InputSlot()
    Output = OutputSlot()

    def setupOutputs(self):
        self.Output.meta.assignFrom(self.Input.meta)

    def execute(self, slot, subindex, roi, result):
        # Save memory: use result as a temporary
        self.Input(roi.start, roi.stop).writeInto(result).wait()
        volume_max = numpy.max(result)
        volume_min = numpy.min(result)

        # result[...] = (result - volume_min) * 255.0 / (volume_max-volume_min)
        # Avoid temporaries...
        result[:] -= volume_min
        result[:] *= 255.0
        result[:] /= volume_max - volume_min
        return result

    def propagateDirty(self, slot, subindex, roi):
        self.Output.setDirty(roi.start, roi.stop)


class OpSimpleBlockwiseWatershed(Operator):
    Input = InputSlot()
    Output = OutputSlot()

    DoAgglo = InputSlot(value=1)
    SizeRegularizer = InputSlot(value=0.5)
    ReduceTo = InputSlot(value=0.2)

    def setupOutputs(self):
        self.Output.meta.assignFrom(self.Input.meta)
        self.Output.meta.dtype = numpy.uint32

    def execute(self, slot, subindex, roi, result):
        if tuple(roi.stop - roi.start) != self.Output.meta.shape:
            raise ValueError("Blockwise Watershed must be run on the entire volume")

        if self.Input.meta.getAxisKeys() != list("txyzc"):
            raise ValueError(f"Unsupported input axis keys {self.Input.meta.getAxisKeys()}")

        if self.Input.meta.getTaggedShape()["z"] > 1:
            result_idx = numpy.s_[0, ..., 0]
        else:
            result_idx = numpy.s_[0, ..., 0, 0]

        input_ = self.Input(roi.start, roi.stop).wait().squeeze()
        if input_.ndim not in (2, 3):
            raise ValueError(f"Input shape {input_.shape} has an invalid number of non-singleton dimensions")

        with Timer() as timer:
            logger.info("Run block-wise watershed in %dd", input_.ndim)

            if self.DoAgglo.value:
                result[result_idx], max_id = watershed_and_agglomerate(
                    input_,
                    max_workers=max(1, Request.global_thread_pool.num_workers),
                    size_regularizer=self.SizeRegularizer.value,
                    reduce_to=self.ReduceTo.value,
                )
            else:
                result[result_idx], max_id = vigra.analysis.watershedsNew(input_)

            logger.info("done %d", max_id)
            logger.info("Blockwise Watershed took %f seconds", timer.seconds())

        return result

    def propagateDirty(self, slot, subindex, roi):
        self.Output.setDirty(slice(None))


class OpMstSegmentorProvider(Operator):
    Image = InputSlot()
    LabelImage = InputSlot()

    MST = OutputSlot(stype="object")

    def __init__(self, applet, *args, **kwargs):
        super(OpMstSegmentorProvider, self).__init__(*args, **kwargs)
        self.applet = applet

    def setupOutputs(self):
        self.MST.meta.shape = (1,)
        self.MST.meta.dtype = object

    def execute(self, slot, subindex, roi, result):
        assert slot == self.MST, "Invalid output slot: {}".format(slot.name)

        # first thing, show the user that we are waiting for computations to finish
        self.applet.progressSignal(-1)
        try:
            volume_feat = self.Image(*roiFromShape(self.Image.meta.shape)).wait()
            labelVolume = self.LabelImage(*roiFromShape(self.LabelImage.meta.shape)).wait()

            self.applet.progress = 0

            def updateProgressBar(x):
                # send signal iff progress is significant
                if x - self.applet.progress > 1 or x == 100:
                    self.applet.progressSignal(x)
                    self.applet.progress = x

            # mst= MSTSegmentor(labelVolume[0,...,0],
            #                  numpy.asarray(volume_feat[0,...,0], numpy.float32),
            #                  edgeWeightFunctor = "minimum",
            #                  progressCallback = updateProgressBar)
            ##mst.raw is not set here in order to avoid redundant data storage
            # mst.raw = None

            newMst = WatershedSegmentor(
                labelVolume[0, ..., 0],
                numpy.asarray(volume_feat[0, ..., 0], numpy.float32),
                edgeWeightFunctor="minimum",
                progressCallback=updateProgressBar,
            )

            # Output is of shape 1
            # result[0] = mst
            newMst.raw = None
            result[0] = newMst
            return result

        finally:
            self.applet.progressSignal(100)

    def propagateDirty(self, slot, subindex, roi):
        self.MST.setDirty(slice(None))


class OpPreprocessing(Operator):
    """
    The top-level operator for the pre-procession applet
    """

    name = "Preprocessing"

    # Image before preprocess
    OverlayData = InputSlot(optional=True)
    InputData = InputSlot()
    Sigma = InputSlot(value=1.6)
    Filter = InputSlot(value=0)

    DoAgglo = InputSlot(value=1)
    SizeRegularizer = InputSlot(value=0.5)
    ReduceTo = InputSlot(value=0.2)

    # Image after preprocess
    PreprocessedData = OutputSlot()

    # Display outputs
    FilteredImage = OutputSlot()
    WatershedImage = OutputSlot()
    WatershedSourceImage = OutputSlot()

    #                                                                                                                         +-> WatershedImage
    #                                                                                                                        /
    # InputData +->                                                  +-> OpSimpleBlockwiseWatershed --->-opWatershedCache +-> opMstProvider +-> [via execute()] +-> PreprocessedData
    #              \                                                 |                                       /
    # Sigma +-----> opFilter +-> opFilterNormalize +-> opFilterCache +--------------------------------------+
    #              /                                                 \
    # Filter +----+                                                   +-> FilteredImage

    # *note: Raw/Input filters used for inversion and smoothing only.

    def __init__(self, *args, **kwargs):
        super(OpPreprocessing, self).__init__(*args, **kwargs)
        self.cachedResult = [None]
        self.applet = self.parent.parent.preprocessingApplet

        self.hasUnsavedData = False  # read by preprocessingSerializer
        self._dirty = False  # to avoid generating new MST unless user has changed settings
        self.deserialized = False

        self.cachedSigma = None  # keep settings of last preprocess execute
        self.cachedFilter = None  # for saving in project file
        self.cachedDoAgglo = None
        self.cachedSizeRegularizer = None
        self.cachedReduceTo = None

        self._opFilter = OpFilter(parent=self)
        self._opFilter.Input.connect(self.InputData)
        self._opFilter.Sigma.connect(self.Sigma)
        self._opFilter.Filter.connect(self.Filter)

        self._opFilterNormalize = OpNormalize255(parent=self)
        self._opFilterNormalize.Input.connect(self._opFilter.Output)

        self._opFilterCache = OpBlockedArrayCache(parent=self)

        self._opWatershed = OpSimpleBlockwiseWatershed(parent=self)
        self._opWatershed.DoAgglo.connect(self.DoAgglo)
        self._opWatershed.ReduceTo.connect(self.ReduceTo)
        self._opWatershed.SizeRegularizer.connect(self.SizeRegularizer)
        self._opWatershed.Input.connect(self._opFilterCache.Output)

        self._opWatershedCache = OpBlockedArrayCache(parent=self)

        self._opMstProvider = OpMstSegmentorProvider(self.applet, parent=self)
        self._opMstProvider.Image.connect(self._opFilterCache.Output)
        self._opMstProvider.LabelImage.connect(self._opWatershedCache.Output)

        self._opWatershedSourceCache = OpBlockedArrayCache(parent=self)

        # Display slots
        self.FilteredImage.connect(self._opFilterCache.Output)
        self.WatershedImage.connect(self._opWatershedCache.Output)

        self.InputData.notifyReady(self._checkConstraints)
        self.OverlayData.notifyReady(self._checkConstraints)

    def _checkConstraints(self, *args):
        input_slot = self.InputData
        if not input_slot.ready():
            return

        if input_slot.meta.getTaggedShape()["c"] != 1:
            raise DatasetConstraintError(
                "Carving",
                "Input image must have exactly one channel. "
                + f"You attempted to add a dataset with {input_slot.meta.getTaggedShape()['c']} channels",
            )
        if input_slot.meta.getTaggedShape()["t"] != 1:
            raise DatasetConstraintError(
                "Carving",
                "Input image must not have more than one time point. "
                + f"You attempted to add a dataset with {input_slot.meta.getTaggedShape()['t']} time points",
            )

        # Errors for developers
        assert len(input_slot.meta.shape) == 5, f"was expecting a 5D dataset, got shape={input_slot.meta.shape}"
        assert all(input_slot.meta.axistags[i].isSpatial() for i in range(1, 4)), "2nd to 4th axes must be spatial"

        if self.OverlayData.ready() and not eq_shapes(
            self.OverlayData.meta.getTaggedShape(), input_slot.meta.getTaggedShape()
        ):
            raise DatasetConstraintError(
                "Carving",
                "Overlay image must have the same shape as raw data (different channels are ok). "
                + f"Your raw data has shape={input_slot.meta.shape} but your overlay has shape {self.OverlayData.meta.shape}",
            )

    def setupOutputs(self):
        self.PreprocessedData.meta.shape = (1,)
        self.PreprocessedData.meta.dtype = object

        self._opFilterCache.BlockShape.setValue(self.InputData.meta.shape)
        self._opFilterCache.Input.connect(self._opFilterNormalize.Output)

        self._opWatershedSourceCache.BlockShape.setValue(self.InputData.meta.shape)
        self._opWatershedSourceCache.Input.connect(self._opWatershed.Input)

        self.WatershedSourceImage.connect(self._opWatershedSourceCache.Output)

        self._opWatershedCache.BlockShape.setValue(self._opWatershed.Output.meta.shape)
        self._opWatershedCache.Input.connect(self._opWatershed.Output)

    def execute(self, slot, subindex, roi, result):
        assert slot == self.PreprocessedData, "Invalid output slot"
        if not self._dirty and self.cachedResult[0] is not None:
            return self.cachedResult

        mst = self._opMstProvider.MST.value

        self.cachedSigma = self.Sigma.value
        self.cachedFilter = self.Filter.value
        self.cachedDoAgglo = self.DoAgglo.value
        self.cachedSizeRegularizer = self.SizeRegularizer.value
        self.cachedReduceTo = self.ReduceTo.value

        self.hasUnsavedData = True
        self._dirty = False

        self.cachedResult = result

        result[0] = mst

        # Signal downstream that a new MST has been created.
        # Otherwise opCarving.propagateDirty does not get called to carry
        # over existing user labels into the new MST.
        self.PreprocessedData.setDirty(slice(None))
        return result

    def propagateDirty(self, slot, subindex, roi):
        if slot == self.InputData:
            # complete restart
            # No values will be reused any more
            self.cachedSigma = None
            self.cachedFilter = None
            self.cachedDoAgglo = None
            self.cachedSizeRegularizer = None
            self.cachedReduceTo = None
            self.cachedResult = [None]

        self._dirty = True
