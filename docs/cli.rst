Command-Line Interface
======================

Lexoid ships with a ``lexoid`` command (installed as a console script) for
parsing documents without writing Python code. You can also invoke it via
the module form ``python -m lexoid``.

.. code-block:: bash

    lexoid --help
    python -m lexoid --help

Commands
--------

The CLI exposes three sub-commands:

* ``lexoid parse`` — Convert a document into markdown (or JSON with metadata).
* ``lexoid schema`` — Extract structured data conforming to a JSON schema.
* ``lexoid latex`` — Convert a document into LaTeX.

Common options
^^^^^^^^^^^^^^

Available across all sub-commands:

* ``--input, -i`` (required): Path to an input file (PDF, image, HTML, DOCX, XLSX, PPTX, CSV, TXT, audio) or a URL (``http://``, ``https://``).
* ``--output, -o``: Path to an output file. If omitted, output goes to stdout (clean — status messages are written to stderr so output can be piped).
* ``--verbose, -v``: Enable detailed logging.
  Page progress is reported as ``Progress: page 3/10 completed``. Since parsing
  is parallel, messages can arrive out of page order. Set
  ``--pages-per-split 1`` to receive a log as soon as each individual page
  finishes.

``lexoid parse``
^^^^^^^^^^^^^^^^

.. code-block:: bash

    lexoid parse --input document.pdf
    lexoid parse --input document.pdf --output output.md
    lexoid parse --input document.pdf --format json --output result.json
    lexoid parse --input document.pdf --parser-type STATIC_PARSE
    lexoid parse --input document.pdf --model gpt-4o

Options:

* ``--parser-type, -p``: ``AUTO`` (default), ``LLM_PARSE``, or ``STATIC_PARSE``.
* ``--model, -m``: LLM model name. Default: ``gemini-2.5-flash``.
* ``--pages-per-split``: Pages per chunk. Default: ``4``.
* ``--max-processes``: Parallel processes. Default: ``4``.
* ``--framework``: Static parsing framework — ``pdfplumber`` or ``paddleocr``.
* ``--format``: ``markdown`` (default; raw markdown text) or ``json`` (full result with segments, metadata, and token usage).
* ``--api``: API provider override. One of ``openai``, ``gemini``, ``anthropic``, ``mistral``, ``together``, ``huggingface``, ``openrouter``, ``fireworks``, ``deepseek``, ``minimax``, ``ollama``. If omitted, inferred from the model name.

``lexoid schema``
^^^^^^^^^^^^^^^^^

Extract structured data using a JSON schema. The schema can be passed as a
file path or as an inline JSON string.

.. code-block:: bash

    # Inline schema
    lexoid schema \
      --input document.pdf \
      --schema '{"type": "object", "properties": {"title": {"type": "string"}}}' \
      --output result.json

    # Schema from file
    lexoid schema --input document.pdf --schema schema.json --output result.json

    # Specify model and API explicitly
    lexoid schema --input document.pdf --schema schema.json --api openai --model gpt-4o

Options:

* ``--schema, -s`` (required): JSON schema — file path or inline JSON.
* ``--model, -m``: LLM model. Default: ``gpt-4o-mini``.
* ``--api``: API provider (auto-detected from model name if omitted).
* ``--example-schema``: Example data (JSON string or file path) illustrating a filled schema.
* ``--fill-single-schema``: Produce a single schema instance for the whole document instead of one per page.

``lexoid latex``
^^^^^^^^^^^^^^^^

.. code-block:: bash

    lexoid latex --input document.pdf
    lexoid latex --input document.pdf --output output.tex
    lexoid latex --input document.pdf --model gpt-6-astra
    lexoid latex --input document.pdf --output raw.tex --ocr paddleocr \
      --render-dpi 240 --evidence-output raw.recognition.json --auto-orient

The command writes page-level progress to stderr after every completed page,
for example: ``Progress: page 3/10 completed``.

With ``--output``, each completed page is saved atomically and receives a
LaTeX comment checkpoint. Resume an interrupted conversion with, for example,
``--start-page 6``. Lexoid requires the existing output to end at page 5 and
checks that the PDF page count still matches, preventing gaps or duplicate
pages.

Options:

* ``--model, -m``: LLM model. Default: ``gpt-6-astra``.
* ``--api``: API provider (auto-detected from model name if omitted).
* ``--start-page``: Resume at this 1-based page. Default: ``1``.
* ``--ocr``: ``none`` (default, legacy vision path) or ``paddleocr`` (hybrid PDF path).
* ``--render-dpi``: Initial hybrid page DPI. Default: ``240``.
* ``--evidence-output``: Hybrid JSON evidence path. With ``--output``, defaults
  to ``<output-stem>.recognition.json``.
* ``--cache-dir``: Directory for resumable render, OCR, table, and draft caches.
* ``--vision-concurrency``: Maximum concurrent vision calls. Default: ``4``.
* ``--resume / --no-resume``: Reuse validated hybrid stage caches. Default: enabled.
* ``--auto-orient / --no-auto-orient``: Normalize page orientation before recognition.
* ``--organize-values``: After conversion, create ``.template.tex`` and an
  editable ``.values.json`` manifest.

The combined runtime uses CPU inference on the macOS Docker host. Paddle
weights download on demand and persist in the configured model-cache volume.
PaddleOCR-VL is used only for unresolved table topology. The vision model
remains responsible for final page TeX; evidence JSON is kept separately.

``lexoid latex-template`` and ``lexoid latex-fill``
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Create a template from an annotated LaTeX file, edit its JSON values, and fill
the template without another LLM request:

.. code-block:: bash

    lexoid latex-template --input output.tex \
      --output output.template.tex --values-output output.values.json
    lexoid latex-fill --template output.template.tex \
      --values output.values.json --output output.edited.tex

API keys
--------

LLM commands require the relevant environment variable to be set
(see :doc:`installation`). The CLI checks for the required key based on
the resolved provider and raises a clear error if it is missing.
