"""Keep acoustic context intact when streaming Qwen codec frames.

The upstream fast wrapper switches to short windows after 32 generated frames.
That changes the acoustic context abruptly. Its codec wrapper also internally
splits at 300 frames, including the reference. Decode the complete prefix with
the decoder directly and emit only the newly committed suffix instead.
"""
from __future__ import annotations

import numpy as np
import time
import hashlib


def decode_complete_utterance(codec_source, decode_prefix, sample_rate, cancel_event=None):
    """For buffered playback, decode the complete acoustic prefix once.

    Keep the same autoregressive tokens, reference and complete history. The
    streaming path remains available when incremental audio is required.
    """
    pieces = []
    source = iter(codec_source)
    started = time.perf_counter()
    try:
        while cancel_event is None or not cancel_event.is_set():
            try:
                codes, _timing = next(source)
            except StopIteration:
                break
            pieces.append(codes)
        if not pieces or (cancel_event is not None and cancel_event.is_set()):
            return
        codec_seconds = time.perf_counter() - started
        started = time.perf_counter()
        audio = np.asarray(decode_prefix(pieces), dtype=np.float32)
        if audio.ndim != 1 or not np.isfinite(audio).all():
            raise RuntimeError('Invalid full-utterance audio')
        if cancel_event is None or not cancel_event.is_set():
            yield audio, sample_rate, {'codec_seconds': codec_seconds,
                                      'acoustic_seconds': time.perf_counter() - started}
    finally:
        close = getattr(source, 'close', None)
        if close:
            close()


def stream_full_prefix(codec_source, decode_prefix, sample_rate, cancel_event=None):
    """decode_prefix receives every generated chunk, never a shortened window."""
    pieces, committed = [], 0
    source = iter(codec_source)
    try:
        while cancel_event is None or not cancel_event.is_set():
            try:
                codes, timing = next(source)
            except StopIteration:
                break
            if cancel_event is not None and cancel_event.is_set():
                break
            pieces.append(codes)
            audio = np.asarray(decode_prefix(pieces), dtype=np.float32)
            if audio.ndim != 1 or len(audio) < committed or not np.isfinite(audio).all():
                raise RuntimeError('Invalid or shrinking full-prefix audio')
            new_audio = audio[committed:]
            committed = len(audio)
            if cancel_event is not None and cancel_event.is_set():
                break
            if new_audio.size:
                yield new_audio, sample_rate, timing
    finally:
        close = getattr(source, 'close', None)
        if close:
            close()


def stable_voice_clone_stream(model, *, text, voice_clone_prompt, max_new_tokens=512,
                              chunk_size=8, cancel_event=None, delivery_mode='streaming', trace_codes=False):
    # Import GPU dependencies only inside the companion process.
    import torch
    from faster_qwen3_tts.streaming import fast_generate_streaming

    with torch.inference_mode():
        m, talker, config, embeds, mask, trailing, pad, ref_codes = model._prepare_generation(
            text=text, language='Chinese', voice_clone_prompt=voice_clone_prompt,
            non_streaming_mode=False)
        tokenizer = m.speech_tokenizer
        decoder = tokenizer.model.decoder
        sr = int(tokenizer.get_output_sample_rate())
        upsample = int(tokenizer.get_decode_upsample_rate())
        reference_samples = len(ref_codes)*upsample if ref_codes is not None else 0
        source = fast_generate_streaming(talker=talker, talker_input_embeds=embeds,
            attention_mask=mask, trailing_text_hiddens=trailing, tts_pad_embed=pad,
            config=config, predictor_graph=model.predictor_graph, talker_graph=model.talker_graph,
            max_new_tokens=max_new_tokens, chunk_size=chunk_size)

        last_digest = None
        def decode_prefix(chunks):
            nonlocal last_digest
            generated = torch.cat(chunks, dim=0)
            if trace_codes:
                last_digest = hashlib.sha256(generated.detach().cpu().numpy().tobytes()).hexdigest()
            codes = torch.cat([ref_codes.to(generated.device), generated], dim=0) if ref_codes is not None else generated
            # Avoid tokenizer.decode's implicit 300-frame split; retain the
            # reference and all generated history for every acoustic decode.
            full = decoder(codes.transpose(0, 1).unsqueeze(0)).reshape(-1)
            expected = len(codes)*upsample
            if len(full) != expected:
                raise RuntimeError(f'Unexpected codec sample count: {len(full)} != {expected}')
            return full[reference_samples:].float().cpu().numpy()

        factory = decode_complete_utterance if delivery_mode == 'buffered' else stream_full_prefix
        stream = factory(source, decode_prefix, sr, cancel_event)
        try:
            for audio, rate, timing in stream:
                if trace_codes:
                    timing = {**timing, 'codec_sha256': last_digest}
                yield audio, rate, timing
        finally:
            stream.close()
