# Golden Transcripts

Each `*.jsonl` file is one scenario. Lines alternate between `request` and
`response` objects:

```json
{"type":"request","message":{...}}
{"type":"response","message":{...},"allow_volatile":[]}
```

`message` is the exact line-delimited JSON-RPC object sent on stdin or read
from stdout. `allow_volatile` lists response paths that replay harnesses may
ignore when future scenarios contain paths, ids, or timings.

Regenerate the deterministic engine chassis corpus with:

```sh
make transcripts
```

Replay validation is part of `make test`; Go drives the real Python engine through
`internal/engineclient`, and Python replays the same corpus through its own loop.
