import tensorflow as tf
import tensorflow.keras as keras
import tensorflow.keras.initializers as initializers

from keras_cv.metrics.coco import iou as iou_lib
from keras_cv.metrics.coco import util
from keras_cv.util import bbox


class COCOBase(keras.metrics.Metric):
    """COCOBase serves as a base for COCORecall and COCOPrecision.

    Args:
        iou_thresholds: defaults to [0.5:0.05:0.95].  Dimension T=len(iou_thresholds), defaults to 10.
        category_ids: no default, users must provide.  K=len(category_ids)
        area_range: area range to consider bounding boxes in. Defaults to all.
        max_detections: number of maximum detections a model is allowed to make.

    Internally the COCOBase class tracks the following values:

    - TruePositives: tf.Tensor with shape [TxK] precision for every evaluation setting.
    - FalsePositives: tf.Tensor with shape [TxK] precision for every evaluation setting.
    - GroundTruthBoxes: tf.Tensor with shape [K] max recall for every evaluation setting.
    """

    def __init__(
        self,
        iou_thresholds=None,
        category_ids=None,
        area_range=(0, 1e9 ** 2),
        max_detections=100,
        **kwargs
    ):
        super().__init__(**kwargs)
        # Initialize parameter values
        self._user_iou_thresholds = iou_thresholds or [
            x / 100.0 for x in range(50, 100, 5)
        ]
        self.iou_thresholds = self._add_constant_weight(
            "iou_thresholds", self._user_iou_thresholds
        )
        # TODO(lukewood): support inference of category_ids based on update_state calls.
        self.category_ids = self._add_constant_weight("category_ids", category_ids)

        self.area_range = area_range
        self.max_detections = max_detections

        # Initialize result counters
        num_thresholds = len(self._user_iou_thresholds)
        num_categories = len(category_ids)

        self.true_positives = self.add_weight(
            name="true_positives",
            shape=(num_thresholds, num_categories),
            dtype=tf.float32,
            initializer=initializers.Zeros(),
        )
        self.false_positives = self.add_weight(
            name="false_positives",
            shape=(num_thresholds, num_categories),
            dtype=tf.float32,
            initializer=initializers.Zeros(),
        )
        self.ground_truth_boxes = self.add_weight(
            name="ground_truth_boxes",
            shape=(num_categories,),
            dtype=tf.float32,
            initializer=initializers.Zeros(),
        )

    def reset_state(self):
        super().reset_state()
        self.true_positives.assign(tf.zeros_like(self.true_positives))
        self.false_positives.assign(tf.zeros_like(self.false_positives))
        self.ground_truth_boxes.assign(tf.zeros_like(self.ground_truth_boxes))

    @tf.function()
    def update_state(self, y_true, y_pred, sample_weight=None):
        """
        Args:
            y_true: a bounding box Tensor in corners format.
            y_pred: a bounding box Tensor in corners format.
            sample_weight: Currently unsupported.
        """
        if sample_weight:
            raise NotImplementedError(
                "sample_weight is not yet supported in keras_cv COCO metrics."
            )
        num_images = y_true.shape[0]

        num_thresholds = self.iou_thresholds.shape[0]
        num_categories = self.category_ids.shape[0]

        # Sort by bbox.CONFIDENCE to make maxDetections easy to compute.
        y_pred = util.sort_bboxes(y_pred, axis=bbox.CONFIDENCE)
        true_positives_update = tf.zeros_like(self.true_positives)
        false_positives_update = tf.zeros_like(self.false_positives)
        ground_truth_boxes_update = tf.zeros_like(self.ground_truth_boxes)

        for img in tf.range(num_images):
            sentinel_filtered_y_true = util.filter_out_sentinels(y_true[img])
            sentinel_filtered_y_pred = util.filter_out_sentinels(y_pred[img])

            area_filtered_y_true = util.filter_boxes_by_area_range(
                sentinel_filtered_y_true, self.area_range[0], self.area_range[1]
            )
            # TODO(lukewood): try filtering area after max dts.
            area_filtered_y_pred = util.filter_boxes_by_area_range(
                sentinel_filtered_y_pred, self.area_range[0], self.area_range[1]
            )

            for k_i in tf.range(num_categories):
                category = self.category_ids[k_i]

                category_filtered_y_pred = util.filter_boxes(
                    area_filtered_y_pred, value=category, axis=bbox.CLASS
                )

                detections = category_filtered_y_pred
                if self.max_detections < tf.shape(category_filtered_y_pred)[0]:
                    detections = category_filtered_y_pred[: self.max_detections]

                ground_truths = util.filter_boxes(
                    area_filtered_y_true, value=category, axis=bbox.CLASS
                )

                ious = iou_lib.compute_ious_for_image(ground_truths, detections)

                for t_i in tf.range(num_thresholds):
                    threshold = self.iou_thresholds[t_i]
                    pred_matches = self._match_boxes(
                        ground_truths, detections, threshold, ious
                    )

                    indices = [t_i, k_i]
                    true_positives = tf.cast(pred_matches != -1, tf.float32)
                    false_positives = tf.cast(pred_matches == -1, tf.float32)

                    true_positives_sum = tf.math.reduce_sum(true_positives, axis=-1)
                    false_positives_sum = tf.math.reduce_sum(
                        false_positives, axis=-1
                    )

                    true_positives_update = tf.tensor_scatter_nd_add(
                        true_positives_update, [indices], [true_positives_sum]
                    )
                    false_positives_update = tf.tensor_scatter_nd_add(
                        false_positives_update, [indices], [false_positives_sum]
                    )

                ground_truth_boxes_update = tf.tensor_scatter_nd_add(
                    ground_truth_boxes_update,
                    [[k_i]],
                    [tf.cast(tf.shape(ground_truths)[0], tf.float32)],
                )

        self.true_positives.assign_add(true_positives_update)
        self.false_positives.assign_add(false_positives_update)
        self.ground_truth_boxes.assign_add(ground_truth_boxes_update)

    def _match_boxes(self, y_true, y_pred, threshold, ious):
        """matches bounding boxes from y_true to boxes in y_pred.

        Args:
            y_true: bounding box tensor of shape [num_boxes, 4+].
            y_pred: bounding box tensor of shape [num_boxes, 4+].
            threshold: minimum IoU for a pair to be considered a match.
            ious: lookup table from [y_true, y_pred] => IoU.
        Returns:
            indices of matches between y_pred and y_true.
        """
        num_true = tf.shape(y_true)[0]
        num_pred = tf.shape(y_pred)[0]

        gt_matches = tf.TensorArray(
            tf.int32,
            size=num_true,
            dynamic_size=False,
            infer_shape=False,
            element_shape=(),
        )
        pred_matches = tf.TensorArray(
            tf.int32,
            size=num_pred,
            dynamic_size=False,
            infer_shape=False,
            element_shape=(),
        )
        for i in tf.range(num_true):
            gt_matches = gt_matches.write(i, -1)
        for i in tf.range(num_pred):
            pred_matches = pred_matches.write(i, -1)

        for detection_idx in tf.range(num_pred):
            match_index = -1
            iou = tf.math.minimum(threshold, 1 - 1e-10)

            for gt_idx in tf.range(num_true):
                if gt_matches.gather([gt_idx]) > -1:
                    continue
                # TODO(lukewood): update clause to account for gtIg
                # if m > -1 and gtIg[m] == 0 and gtIg[gind] == 1:

                if not ious[gt_idx, detection_idx] >= threshold:
                    continue
                iou = ious[gt_idx, detection_idx]
                match_index = gt_idx

            # Write back the match indices
            pred_matches = pred_matches.write(detection_idx, match_index)
            if match_index == -1:
                continue
            gt_matches = gt_matches.write(match_index, detection_idx)
        return pred_matches.stack()

    def result(self):
        raise NotImplementedError("COCOBase subclasses must implement `result()`.")

    def _add_constant_weight(self, name, values, shape=None, dtype=tf.float32):
        shape = shape or (len(values),)
        return self.add_weight(
            name=name,
            shape=shape,
            initializer=initializers.Constant(tf.cast(tf.constant(values), dtype)),
            dtype=dtype,
        )
