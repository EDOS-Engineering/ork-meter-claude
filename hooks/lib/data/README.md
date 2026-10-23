The compressed vocabulary is the OpenAI `o200k_base` vocabulary from
https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken,
distributed with OpenAI's MIT-licensed tiktoken encoding. Its uncompressed
SHA-256 is `446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d`.
The splitting pattern and special-token definitions in `tokens.py` are from
tiktoken 0.9.0 and are unchanged in the pinned tiktoken 0.14.0. See `LICENSE-tiktoken.txt`.

This is public tokenizer data, contains no runtime captures or credentials,
and adds no model instructions. The runtime verifies its hash and constructs
the encoding locally without downloads or model-name resolution. Token counts
remain a surrogate for provider tokenization, framing and billing.
