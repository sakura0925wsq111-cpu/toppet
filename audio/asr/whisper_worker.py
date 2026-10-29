"""Private stdin/stdout ASR worker, launched only by IsolatedWhisperEngine."""
import argparse
import base64
from contextlib import redirect_stdout
import json
import sys

from .whisper_asr import FasterWhisperEngine


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--model', default='small')
    parser.add_argument('--device', default='cpu')
    parser.add_argument('--compute-type', default='int8')
    args = parser.parse_args()
    with redirect_stdout(sys.stderr):
        engine = FasterWhisperEngine(args.model, args.device, args.compute_type)
        engine.preload()
    print(json.dumps({'ready': True}), flush=True)
    for line in sys.stdin:
        try:
            pcm = base64.b64decode(json.loads(line)['pcm'], validate=True)
            with redirect_stdout(sys.stderr):
                text = engine.transcribe_pcm(pcm)
            result = {'text': text}
        except Exception as exc:
            result = {'error': str(exc)}
        print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
