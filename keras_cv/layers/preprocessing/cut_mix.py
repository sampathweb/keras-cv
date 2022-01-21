# Copyright 2022 The KerasCV Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import tensorflow as tf
import tensorflow.keras as keras
import tensorflow.keras.layers as layers
from tensorflow.python.platform import tf_logging as logging


class CutMix(layers.Layer):
    """CutMix implements the CutMix data augmentation technique.

    Args:
        rate: Float between 0 and 1.  The fraction of samples to augment.
        alpha: Float between 0 and 1.  Inverse scale parameter for the gamma distribution.
            This controls the shape of the distribution from which the smoothing values are
            sampled.  Defaults 1.0, which is a recommended value when training an imagenet1k
            classification model.
        label_smoothing: Float in [0, 1]. When > 0, label values are smoothed, meaning the
            confidence on label values are relaxed. e.g. label_smoothing=0.2 means that we
            will use a value of 0.1 for label 0 and 0.9 for label 1.  Defaults 0.0.
    References:
       [CutMix paper]( https://arxiv.org/abs/1905.04899).

    Sample usage:
    ```python
    (images, labels), _ = tf.keras.datasets.cifar10.load_data()
    cutmix = keras_cv.layers.preprocessing.cut_mix.CutMix(10)
    augmented_images, updated_labels = cutmix(images, labels)
    ```
    """

    def __init__(self, rate, label_smoothing=0.0, alpha=1.0, seed=None, **kwargs):
        super().__init__(**kwargs)
        self.alpha = alpha
        self.rate = rate
        self.label_smoothing = label_smoothing
        self.seed = seed

    @staticmethod
    def _sample_from_beta(alpha, beta, shape):
        sample_alpha = tf.random.gamma(shape, 1.0, beta=alpha)
        sample_beta = tf.random.gamma(shape, 1.0, beta=beta)
        return sample_alpha / (sample_alpha + sample_beta)

    def call(self, images, labels):
        """call method for the CutMix layer.

        Args:
            images: Tensor representing images of shape [batch_size, width, height, channels], with dtype tf.float32.
            labels: One hot encoded tensor of labels for the images, with dtype tf.float32.
        Returns:
            images: augmented images, same shape as input.
            labels: updated labels with both label smoothing and the cutmix updates applied.
        """

        if tf.shape(images)[0] == 1:
            logging.warning(
                "CutMix received a single image to `call`.  The layer relies on combining multiple examples, "
                "and as such will not behave as expected.  Please call the layer with 2 or more samples."
            )

        augment_cond = tf.less(
            tf.random.uniform(shape=[], minval=0.0, maxval=1.0), self.rate
        )
        # pylint: disable=g-long-lambda
        cutmix_augment = lambda: self._update_labels(*self._cutmix(images, labels))
        no_augment = lambda: (images, self._smooth_labels(labels))
        return tf.cond(augment_cond, cutmix_augment, no_augment)

    def _cutmix(self, images, labels):
        """Apply cutmix."""
        input_shape = tf.shape(images)
        batch_size, image_height, image_width = (
            input_shape[0],
            input_shape[1],
            input_shape[2],
        )

        permutation_order = tf.random.shuffle(
            tf.range(0, batch_size), seed=self.seed
        )
        lambda_sample = CutMix._sample_from_beta(
            self.alpha, self.alpha, (batch_size,)
        )

        ratio = tf.math.sqrt(1 - lambda_sample)

        cut_height = tf.cast(
            ratio * tf.cast(image_height, dtype=tf.float32), dtype=tf.int32
        )
        cut_width = tf.cast(
            ratio * tf.cast(image_height, dtype=tf.float32), dtype=tf.int32
        )

        random_center_height = tf.random.uniform(
            shape=[batch_size], minval=0, maxval=image_height, dtype=tf.int32
        )
        random_center_width = tf.random.uniform(
            shape=[batch_size], minval=0, maxval=image_width, dtype=tf.int32
        )

        bbox_area = cut_height * cut_width
        lambda_sample = 1.0 - bbox_area / (image_height * image_width)
        lambda_sample = tf.cast(lambda_sample, dtype=tf.float32)

        images = tf.map_fn(
            lambda x: _fill_rectangle(*x),
            (
                images,
                random_center_width,
                random_center_height,
                cut_width // 2,
                cut_height // 2,
                tf.gather(images, permutation_order),
            ),
            fn_output_signature=tf.TensorSpec.from_tensor(images[0]),
        )

        return images, labels, lambda_sample, permutation_order

    def _update_labels(self, images, labels, lambda_sample, permutation_order):
        labels_smoothed = self._smooth_labels(labels)
        cutout_labels = tf.gather(labels, permutation_order)

        lambda_sample = tf.reshape(lambda_sample, [-1, 1])
        labels = (
            lambda_sample * labels_smoothed + (1.0 - lambda_sample) * cutout_labels
        )
        return images, labels

    def _smooth_labels(self, labels):
        label_smoothing = self.label_smoothing or 0.0
        off_value = label_smoothing / tf.cast(tf.shape(labels)[1], tf.float32)
        on_value = 1.0 - label_smoothing + off_value
        return labels * on_value + (1 - labels) * off_value


def _fill_rectangle(
    image, center_width, center_height, half_width, half_height, replace=None
):
    """Fill a rectangle in a given image using the value provided in replace.

    Args:
        image: the starting image to fill the rectangle on.
        center_width: the X center of the rectangle to fill
        center_height: the Y center of the rectangle to fill
        half_width: 1/2 the width of the resulting rectangle
        half_height: 1/2 the height of the resulting rectangle
        replace: The value to fill the rectangle with.  Accepts a Tensor,
            Constant, or None.
    Returns:
        image: the modified image with the chosen rectangle filled.
    """
    image_shape = tf.shape(image)
    image_height = image_shape[0]
    image_width = image_shape[1]

    lower_pad = tf.maximum(0, center_height - half_height)
    upper_pad = tf.maximum(0, image_height - center_height - half_height)
    left_pad = tf.maximum(0, center_width - half_width)
    right_pad = tf.maximum(0, image_width - center_width - half_width)

    cutout_shape = [
        image_height - (lower_pad + upper_pad),
        image_width - (left_pad + right_pad),
    ]
    padding_dims = [[lower_pad, upper_pad], [left_pad, right_pad]]
    mask = tf.pad(
        tf.zeros(cutout_shape, dtype=image.dtype), padding_dims, constant_values=1
    )
    mask = tf.expand_dims(mask, -1)

    if replace is None:
        fill = tf.random.normal(image_shape, dtype=image.dtype)
    elif isinstance(replace, tf.Tensor):
        fill = replace
    else:
        fill = tf.ones_like(image, dtype=image.dtype) * replace
    image = tf.where(tf.equal(mask, 0), fill, image)
    return image
