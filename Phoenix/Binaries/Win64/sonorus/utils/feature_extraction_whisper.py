# Copyright 2022 The HuggingFace Inc. team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
Whisper feature extractor - standalone version extracted from HuggingFace transformers.
Pure numpy, no torch or transformers dependency required.
"""

import numpy as np

from .audio_utils import mel_filter_bank, spectrogram, window_function


class WhisperFeatureExtractor:
    """
    Extracts log-mel spectrogram features from audio, matching Whisper's expected input format.
    """

    def __init__(
        self,
        feature_size=80,
        sampling_rate=16000,
        hop_length=160,
        chunk_length=30,
        n_fft=400,
        padding_value=0.0,
        dither=0.0,
    ):
        self.n_fft = n_fft
        self.hop_length = hop_length
        self.chunk_length = chunk_length
        self.n_samples = chunk_length * sampling_rate
        self.nb_max_frames = self.n_samples // hop_length
        self.sampling_rate = sampling_rate
        self.padding_value = padding_value
        self.dither = dither
        self.mel_filters = mel_filter_bank(
            num_frequency_bins=1 + n_fft // 2,
            num_mel_filters=feature_size,
            min_frequency=0.0,
            max_frequency=8000.0,
            sampling_rate=sampling_rate,
            norm="slaney",
            mel_scale="slaney",
        )

    @staticmethod
    def zero_mean_unit_var_norm(input_values, attention_mask=None, padding_value=0.0):
        """Zero-mean and unit-variance normalize audio."""
        if attention_mask is not None:
            attention_mask = np.array(attention_mask, np.int32)
            normed = []
            for vector, length in zip(input_values, attention_mask.sum(-1)):
                normed_slice = (vector - vector[:length].mean()) / np.sqrt(vector[:length].var() + 1e-7)
                if length < normed_slice.shape[0]:
                    normed_slice[length:] = padding_value
                normed.append(normed_slice)
        else:
            normed = [(x - x.mean()) / np.sqrt(x.var() + 1e-7) for x in input_values]
        return normed

    def _extract_fbank_features(self, waveform_batch: np.ndarray) -> np.ndarray:
        """Compute log-mel spectrogram features matching Whisper's format."""
        log_spec_batch = []
        for waveform in waveform_batch:
            log_spec = spectrogram(
                waveform,
                window_function(self.n_fft, "hann"),
                frame_length=self.n_fft,
                hop_length=self.hop_length,
                power=2.0,
                dither=self.dither,
                mel_filters=self.mel_filters,
                log_mel="log10",
            )
            log_spec = log_spec[:, :-1]
            log_spec = np.maximum(log_spec, log_spec.max() - 8.0)
            log_spec = (log_spec + 4.0) / 4.0
            log_spec_batch.append(log_spec)
        return np.array(log_spec_batch)

    def __call__(
        self,
        raw_speech,
        sampling_rate=None,
        return_tensors=None,
        padding="max_length",
        max_length=None,
        truncation=True,
        do_normalize=False,
        **kwargs,
    ):
        """
        Extract features from audio.

        Args:
            raw_speech: numpy array of audio samples (1D) or list of arrays
            sampling_rate: expected sample rate (for validation)
            padding: "max_length" to pad to max_length
            max_length: max number of samples
            truncation: whether to truncate to max_length
            do_normalize: zero-mean unit-variance normalize

        Returns:
            dict with "input_features" key containing numpy array
        """
        # Handle single audio vs batch
        if isinstance(raw_speech, np.ndarray) and raw_speech.ndim == 1:
            raw_speech = [raw_speech]
        elif isinstance(raw_speech, (list, tuple)) and isinstance(raw_speech[0], (int, float)):
            raw_speech = [np.array(raw_speech, dtype=np.float32)]

        # Ensure float32
        raw_speech = [np.asarray(s, dtype=np.float32) for s in raw_speech]

        # Determine target length
        target_length = max_length if max_length else self.n_samples

        # Pad or truncate
        processed = []
        attention_masks = []
        for audio in raw_speech:
            actual_len = len(audio)
            if truncation and actual_len > target_length:
                audio = audio[:target_length]
                actual_len = target_length

            mask = np.ones(target_length, dtype=np.int32)
            if actual_len < target_length:
                padded = np.full(target_length, self.padding_value, dtype=np.float32)
                padded[:actual_len] = audio
                mask[actual_len:] = 0
                audio = padded

            processed.append(audio)
            attention_masks.append(mask)

        # Normalize if requested
        if do_normalize:
            processed = self.zero_mean_unit_var_norm(
                processed,
                attention_mask=attention_masks,
                padding_value=self.padding_value,
            )

        # Extract features
        waveform_batch = np.array(processed, dtype=np.float32)
        input_features = self._extract_fbank_features(waveform_batch)

        return {"input_features": input_features.astype(np.float32)}
