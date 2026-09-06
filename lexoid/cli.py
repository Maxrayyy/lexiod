#!/usr/bin/env python3
"""Command-line interface for Lexoid document parsing library."""

import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional

import click
from loguru import logger

from lexoid.api import ParserType
from lexoid.api import parse as api_parse
from lexoid.api import parse_to_latex as api_parse_to_latex
from lexoid.api import parse_with_schema as api_parse_with_schema
from lexoid.core.latex_template import (
    LatexTemplateError,
    fill_file as fill_latex_template_file,
    organize_file as organize_latex_file,
)
from lexoid.core.prompt_templates import LATEX_CHECKBOX_FIELD_COMMAND


API_KEY_ENV_VARS = {
    "openai": "OPENAI_API_KEY",
    "gemini": "GOOGLE_API_KEY",  # Use 'gemini' as provider name, GOOGLE_API_KEY for credentials
    "anthropic": "ANTHROPIC_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "together": "TOGETHER_API_KEY",
    "huggingface": "HUGGINGFACEHUB_API_TOKEN",
    "openrouter": "OPENROUTER_API_KEY",
    "fireworks": "FIREWORKS_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "minimax": "MINIMAX_API_KEY",
    "ollama": None,
    "local": None,  # Local models don't require an API key
}
API_PROVIDER_CHOICES = [k for k in API_KEY_ENV_VARS.keys() if k != "local"]
from lexoid.core.model_config import resolve_model

LATEX_PAGE_CHECKPOINT_RE = re.compile(
    r"(?m)^\s*% LEXOID_PAGE_COMPLETED:\s*(?P<page>\d+)/(?P<total>\d+)\s*$"
)
LATEX_HANDWRITTEN_COMMAND = r"\newcommand{\handwritten}[1]{#1}"
LATEX_FIELD_VALUE_COMMAND = r"\newcommand{\fieldvalue}[1]{#1}"
def timestamped_status(message: str) -> str:
    """Prefix a user-facing status message with a local ISO-8601 timestamp."""
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    return f"[{timestamp}] {message}"


def status_echo(message: str, **kwargs) -> None:
    """Write a timestamped status line to stderr."""
    click.echo(timestamped_status(message), err=True, **kwargs)


def status_secho(message: str, **kwargs) -> None:
    """Write a styled, timestamped status line to stderr."""
    click.secho(timestamped_status(message), err=True, **kwargs)


def validate_input_file(path: str) -> str:
    """Validate that input is a file path or supported URL.

    Returns the path/URL as a string. URLs (http://, https://) are passed through.
    File paths are validated to exist and be readable.
    """
    if path.startswith(("http://", "https://")):
        return path  # Pass URLs through as-is; downstream handles them

    file_path = Path(path).expanduser().resolve()
    if not file_path.exists():
        raise click.FileError(path, hint="File does not exist")
    if not file_path.is_file():
        raise click.UsageError(f"Path is not a file: {path}")
    return str(file_path)


def validate_output_path(path: Optional[str]) -> Optional[Path]:
    """Validate and prepare output path."""
    if path is None:
        return None
    output_path = Path(path).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    return output_path


def configure_logging(verbose: bool) -> None:
    """Enable or disable Lexoid logging."""
    if verbose:
        logger.enable("lexoid")
    else:
        logger.disable("lexoid")


def api_key_env_var(api_provider: str) -> Optional[str]:
    """Return the environment variable used for an API provider."""
    return API_KEY_ENV_VARS.get(api_provider.lower())


def check_api_key(api_provider: str) -> bool:
    """Check if required API key is set for provider."""
    key_name = api_key_env_var(api_provider)
    if key_name and not os.getenv(key_name):
        return False
    return True


def ensure_api_key(api_provider: str, *, required: bool) -> bool:
    """Validate the configured API key for a provider."""
    has_key = check_api_key(api_provider)
    if has_key:
        return True

    if required:
        key_name = api_key_env_var(api_provider) or f"{api_provider.upper()}_API_KEY"
        raise click.ClickException(
            f"API key required for {api_provider.upper()}. "
            f"Please set {key_name} environment variable."
        )

    return False


def infer_api_provider(model: str) -> str:
    """Infer API provider from model name.

    Provider names must match those used in lexoid.core.utils.get_api_provider_for_model().
    Raises ValueError if model is not recognized to match core library behavior.
    """
    model_lower = model.lower()

    # Match logic from lexoid.core.utils.get_api_provider_for_model
    if model_lower.startswith("gemini"):
        return "gemini"
    if model_lower.startswith("gpt"):
        return "openai"
    if model_lower.startswith("meta-llama"):
        if "turbo" in model_lower or model == "meta-llama/Llama-Vision-Free":
            return "together"
        return "huggingface"
    if any(
        model_lower.startswith(prefix) for prefix in ["microsoft", "google", "qwen"]
    ):
        return "openrouter"
    if model_lower.startswith("accounts/fireworks"):
        return "fireworks"
    if model_lower.startswith("claude"):
        return "anthropic"
    if model_lower.startswith("mistral"):
        return "mistral"
    if model_lower.startswith("deepseek"):
        return "deepseek"
    if model_lower.startswith(("minimax", "m2-")):
        return "minimax"
    if "docling" in model_lower:
        return "local"
    if model_lower.startswith("paddlepaddle/paddleocr-vl"):
        return "local"

    raise ValueError(f"Unsupported model: {model}")


def resolve_api_provider(model: str, api: Optional[str] = None) -> str:
    """Resolve the API provider from an explicit override or model name."""
    return api or infer_api_provider(model)


def load_schema_definition(schema: str) -> dict:
    """Load a schema from a path or inline JSON string."""
    schema_path = Path(schema).expanduser()
    if schema_path.exists() and schema_path.is_file():
        try:
            return json.loads(schema_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise click.ClickException(f"Invalid JSON in schema file: {error}")

    try:
        return json.loads(schema)
    except json.JSONDecodeError as error:
        raise click.ClickException(
            f"Schema must be valid JSON (file or inline string): {error}"
        )


def format_parse_output(result: dict, output_format: str) -> str:
    """Format parse output as markdown or JSON."""
    if output_format == "json":
        return json.dumps(result, indent=2, ensure_ascii=False)
    return result["raw"]


def write_output(
    output_path: Optional[Path], content: str, success_message: str
) -> None:
    """Write output to a file or stdout.

    When outputting to stdout, only the content is written to stdout (for clean piping).
    Status messages go to stderr.
    """
    if output_path:
        output_path.write_text(content, encoding="utf-8")
        status_secho(
            f"✅ {success_message} Output saved to: {output_path}", fg="green"
        )
        return

    # Write only content to stdout for clean piping; status to stderr
    click.echo(content)


def get_latex_checkpoint(output_path: Path) -> Optional[tuple[int, int]]:
    """Return the last durable page checkpoint embedded in a LaTeX output."""
    if not output_path.exists():
        return None
    matches = list(LATEX_PAGE_CHECKPOINT_RE.finditer(output_path.read_text("utf-8")))
    if not matches:
        return None
    match = matches[-1]
    return int(match.group("page")), int(match.group("total"))


def write_latex_page(
    output_path: Path, page: int, total_pages: int, page_content: str
) -> None:
    """Atomically persist one completed page without risking the previous output."""
    markers = [(int(match["page"]), int(match["total"]))
               for match in LATEX_PAGE_CHECKPOINT_RE.finditer(page_content)]
    if markers and markers != [(page, total_pages)]:
        raise click.ClickException("Page content has an incorrect or duplicate checkpoint.")
    # This writer owns durable checkpoints, including those supplied by recognizers.
    page_content = LATEX_PAGE_CHECKPOINT_RE.sub("", page_content).rstrip()
    if page == 1:
        existing_content = ""
    else:
        checkpoint = get_latex_checkpoint(output_path)
        if checkpoint is None or checkpoint[0] != page - 1:
            completed = checkpoint[0] if checkpoint else "unknown"
            raise click.ClickException(
                f"Cannot save page {page}: output checkpoint is {completed}, "
                f"expected {page - 1}."
            )
        if checkpoint[1] != total_pages:
            raise click.ClickException(
                "Cannot resume: the PDF page count differs from the existing output."
            )
        existing_content = output_path.read_text(encoding="utf-8").rstrip()

    existing_content = existing_content.replace(r"\end{document}", "").rstrip()
    page_content = page_content.replace(r"\end{document}", "").rstrip()
    separator = "\n\n" if existing_content else ""
    document_content = f"{existing_content}{separator}{page_content}"
    if page == total_pages:
        document_content = f"{document_content}\n\n\\end{{document}}"
    new_content = (
        f"{document_content}\n"
        f"% LEXOID_PAGE_COMPLETED: {page}/{total_pages}\n"
    )
    if (
        r"\newcommand{\handwritten}" not in new_content
        and r"\providecommand{\handwritten}" not in new_content
    ):
        begin_document = r"\begin{document}"
        if begin_document not in new_content:
            raise click.ClickException(
                "Cannot save LaTeX safely: output has no \\begin{document}."
            )
        new_content = new_content.replace(
            begin_document,
            f"{LATEX_HANDWRITTEN_COMMAND}\n{begin_document}",
            1,
        )
    if (
        r"\newcommand{\fieldvalue}" not in new_content
        and r"\providecommand{\fieldvalue}" not in new_content
    ):
        begin_document = r"\begin{document}"
        new_content = new_content.replace(
            begin_document,
            f"{LATEX_FIELD_VALUE_COMMAND}\n{begin_document}",
            1,
        )
    if (
        r"\newcommand{\checkboxfield}" not in new_content
        and r"\providecommand{\checkboxfield}" not in new_content
    ):
        begin_document = r"\begin{document}"
        new_content = new_content.replace(
            begin_document,
            f"{LATEX_CHECKBOX_FIELD_COMMAND}\n{begin_document}",
            1,
        )

    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=output_path.parent,
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_name = temp_file.name
            temp_file.write(new_content)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_name, output_path)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


def show_parse_summary(result: dict, output_format: str, to_file: bool = False) -> None:
    """Display parse metadata for markdown output to stderr (for piping)."""
    if output_format != "markdown" or to_file:
        return

    if result.get("token_usage"):
        status_echo(f"📊 Token Usage: {result['token_usage']}")
    if "parsers_used" in result:
        status_echo(f"🔧 Parsers Used: {result['parsers_used']}")


def handle_cli_error(error: Exception, verbose: bool) -> None:
    """Convert unexpected exceptions to Click errors."""
    if verbose:
        import traceback

        traceback.print_exc()
    raise click.ClickException(str(error))


@click.group(invoke_without_command=False)
@click.version_option()
def app():
    """Lexoid: Convert PDFs, images, web pages, and documents into structured markdown."""
    pass


@app.command()
@click.option(
    "--input",
    "-i",
    required=True,
    type=str,
    help="Path to input file (PDF, image, HTML, DOCX, XLSX, PPTX) or URL (http://, https://)",
)
@click.option(
    "--output",
    "-o",
    type=click.Path(),
    default=None,
    help="Path to output markdown file (default: print to stdout)",
)
@click.option(
    "--parser-type",
    "-p",
    type=click.Choice(["AUTO", "LLM_PARSE", "STATIC_PARSE"], case_sensitive=False),
    default="AUTO",
    help="Parser type (default: AUTO)",
)
@click.option(
    "--model",
    "-m",
    default=None,
    help="LLM model (environment: DEFAULT_LLM; local API: DEFAULT_LOCAL_LM)",
)
@click.option(
    "--pages-per-split",
    type=click.IntRange(min=1),
    default=4,
    help="Number of pages per chunk for processing (default: 4)",
)
@click.option(
    "--max-processes",
    type=click.IntRange(min=1),
    default=4,
    help="Maximum parallel processes (default: 4)",
)
@click.option(
    "--framework",
    type=click.Choice(["pdfplumber", "paddleocr"]),
    default=None,
    help="Static parsing framework (auto-detected if not specified)",
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["markdown", "json"]),
    default="markdown",
    help="Output format: markdown (default, plain markdown text) or json (full result with metadata)",
)
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    help="Enable verbose logging",
)
@click.option(
    "--api",
    type=click.Choice(API_PROVIDER_CHOICES, case_sensitive=False),
    default=None,
    help="API provider override (auto-detected from model if not specified)",
)
def parse(
    input,
    output,
    parser_type,
    model,
    pages_per_split,
    max_processes,
    framework,
    output_format,
    verbose,
    api,
):
    """Parse document and extract markdown content."""
    configure_logging(verbose)

    try:
        input_path = validate_input_file(input)
        output_path = validate_output_path(output)

        parser_enum = ParserType[parser_type.upper()]

        api_provider = None
        if parser_enum == ParserType.LLM_PARSE:
            try:
                model = resolve_model("DEFAULT_LOCAL_LM" if api == "local" else "DEFAULT_LLM", model)
                api_provider = resolve_api_provider(model, api)
            except ValueError as e:
                raise click.ClickException(str(e))

            if not ensure_api_key(api_provider, required=False):
                status_secho(
                    f"⚠️  Warning: API key for {api_provider.upper()} not found",
                    fg="yellow",
                )
                key_name = (
                    api_key_env_var(api_provider) or f"{api_provider.upper()}_API_KEY"
                )
                raise click.ClickException(
                    f"API key required for {api_provider.upper()}. "
                    f"Please set {key_name} environment variable."
                )

        elif parser_enum == ParserType.AUTO:
            if api:
                api_provider = api
                # warn if API key missing but do not fail here; core may choose STATIC_PARSE
                if not ensure_api_key(api_provider, required=False):
                    status_secho(
                        f"⚠️  Warning: API key for {api_provider.upper()} not found",
                        fg="yellow",
                    )

        status_echo("🔄 Parsing document...")
        kwargs = {
            "pages_per_split": pages_per_split,
            "max_processes": max_processes,
            "model": model,
        }
        if api_provider:
            kwargs["api_provider"] = api_provider
        if framework:
            kwargs["framework"] = framework

        try:
            result = api_parse(input_path, parser_enum, **kwargs)
        except ValueError as e:
            raise click.ClickException(str(e))
        except Exception as e:
            # Re-raise click exceptions as-is, convert others
            if isinstance(e, click.ClickException):
                raise
            raise click.ClickException(f"Parsing failed: {str(e)}")

        output_content = format_parse_output(result, output_format)

        write_output(output_path, output_content, "Successfully parsed!")

        show_parse_summary(result, output_format, to_file=output_path is not None)

    except click.ClickException:
        raise
    except Exception as e:
        handle_cli_error(e, verbose)


@app.command()
@click.option(
    "--input",
    "-i",
    required=True,
    type=str,
    help="Path to input file (PDF, image, HTML, DOCX, XLSX, PPTX) or URL (http://, https://)",
)
@click.option(
    "--schema",
    "-s",
    required=True,
    help="JSON schema for extraction (file path or inline JSON string)",
)
@click.option(
    "--output",
    "-o",
    type=click.Path(),
    default=None,
    help="Path to output JSON file (default: print to stdout)",
)
@click.option(
    "--model",
    "-m",
    default=None,
    help="LLM model (environment: LEXOID_SCHEMA_MODEL)",
)
@click.option(
    "--api",
    type=click.Choice(API_PROVIDER_CHOICES, case_sensitive=False),
    default=None,
    help="API provider (auto-detected from model if not specified)",
)
@click.option(
    "--example-schema",
    type=str,
    default=None,
    help="Example data conforming to schema (JSON string or file path); enables example-based extraction",
)
@click.option(
    "--fill-single-schema",
    is_flag=True,
    help="Fill single schema if multiple are provided",
)
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    help="Enable verbose logging",
)
def schema(
    input,
    schema,
    output,
    model,
    api,
    example_schema,
    fill_single_schema,
    verbose,
):
    """Extract structured data from document using JSON schema."""
    configure_logging(verbose)

    try:
        input_path = validate_input_file(input)
        output_path = validate_output_path(output)
        schema_dict = load_schema_definition(schema)

        try:
            model = resolve_model("LEXOID_SCHEMA_MODEL", model)
            api = resolve_api_provider(model, api)
        except ValueError as e:
            raise click.ClickException(str(e))

        ensure_api_key(api, required=True)

        # Load example schema if provided
        example_data = None
        if example_schema:
            example_data = load_schema_definition(example_schema)

        status_echo("🔄 Extracting structured data...")
        try:
            result = api_parse_with_schema(
                input_path,
                schema_dict,
                api=api,
                model=model,
                example_schema=example_data,
                fill_single_schema=fill_single_schema,
            )
        except ValueError as e:
            raise click.ClickException(str(e))
        except Exception as e:
            if isinstance(e, click.ClickException):
                raise
            raise click.ClickException(f"Schema extraction failed: {str(e)}")

        output_json = json.dumps(result, indent=2, ensure_ascii=False)
        write_output(output_path, output_json, "Successfully extracted!")

    except click.ClickException:
        raise
    except Exception as e:
        handle_cli_error(e, verbose)


@app.command()
@click.option(
    "--input",
    "-i",
    required=True,
    type=str,
    help="Path to input file (PDF, image, HTML, DOCX, XLSX, PPTX) or URL (http://, https://)",
)
@click.option(
    "--output",
    "-o",
    type=click.Path(),
    default=None,
    help="Path to output LaTeX file (default: print to stdout)",
)
@click.option(
    "--model",
    "-m",
    default=None,
    help="LLM model (environment: LEXOID_MODEL)",
)
@click.option(
    "--api",
    type=click.Choice(API_PROVIDER_CHOICES, case_sensitive=False),
    default=None,
    help="API provider (auto-detected from model if not specified)",
)
@click.option(
    "--start-page",
    type=click.IntRange(min=1),
    default=1,
    show_default=True,
    help="Start at this 1-based page; existing output must end at the previous page",
)
@click.option(
    "--organize-values",
    is_flag=True,
    help="After conversion, create an editable .template.tex and .values.json",
)
@click.option(
    "--auto-orient/--no-auto-orient",
    default=False,
    show_default=True,
    help="Detect and correct rotated PDF pages before vision processing.",
)
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    help="Enable verbose logging",
)
@click.option("--ocr", type=click.Choice(["none", "paddleocr"]), default="none")
@click.option("--render-dpi", type=click.IntRange(72, 600), default=240)
@click.option("--evidence-output", type=click.Path(), default=None)
@click.option("--cache-dir", type=click.Path(), default=None)
@click.option("--vision-concurrency", type=click.IntRange(1, 32), default=4)
@click.option("--resume/--no-resume", default=True)
def latex(input, output, model, api, start_page, organize_values, auto_orient, verbose,
          ocr, render_dpi, evidence_output, cache_dir, vision_concurrency, resume):
    """Convert document to LaTeX format."""
    configure_logging(verbose)

    try:
        input_path = validate_input_file(input)
        output_path = validate_output_path(output)
        checkpoint = None
        if evidence_output and output_path is None:
            raise click.UsageError("--evidence-output requires --output")
        if ocr == "paddleocr" and output_path:
            evidence_output = evidence_output or str(output_path.with_suffix(".recognition.json"))
            cache_dir = cache_dir or str(output_path.parent / ".recognition-cache")

        if start_page > 1:
            if output_path is None:
                raise click.UsageError("--start-page requires --output")
            checkpoint = get_latex_checkpoint(output_path)
            if checkpoint is None:
                raise click.ClickException(
                    f"Cannot resume at page {start_page}: {output_path} has no "
                    "Lexoid page checkpoint."
                )
            if checkpoint[0] != start_page - 1:
                raise click.ClickException(
                    f"Cannot resume at page {start_page}: output completed through "
                    f"page {checkpoint[0]}; use --start-page {checkpoint[0] + 1}."
                )
        if organize_values and output_path is None:
            raise click.UsageError("--organize-values requires --output")

        try:
            model = resolve_model("LEXOID_MODEL", model)
            api = resolve_api_provider(model, api)
        except ValueError as e:
            raise click.ClickException(str(e))

        ensure_api_key(api, required=True)

        status_echo("🔄 Converting to LaTeX...")
        try:
            result = api_parse_to_latex(
                input_path,
                api=api,
                model=model,
                start_page=start_page,
                auto_orient=auto_orient,
                ocr=ocr, render_dpi=render_dpi, evidence_output=evidence_output,
                cache_dir=cache_dir, vision_concurrency=vision_concurrency, resume=resume,
                expected_total_pages=checkpoint[1] if checkpoint else None,
                page_callback=(
                    lambda page, total, content: (
                        (
                            write_latex_page(output_path, page, total, content),
                            status_echo(
                                f"📄 Progress: page {page}/{total} saved"
                            ),
                        )
                        if output_path
                        else status_echo(
                            f"📄 Progress: page {page}/{total} completed"
                        )
                    )
                ),
            )
        except ValueError as e:
            raise click.ClickException(str(e))
        except Exception as e:
            if isinstance(e, click.ClickException):
                raise
            raise click.ClickException(f"LaTeX conversion failed: {str(e)}")

        if output_path:
            status_secho(
                f"✅ Successfully converted! Output saved to: {output_path}",
                fg="green",
            )
            if organize_values:
                template_path = output_path.with_name(
                    f"{output_path.stem}.template{output_path.suffix}"
                )
                values_path = output_path.with_name(f"{output_path.stem}.values.json")
                try:
                    value_count = organize_latex_file(
                        output_path, template_path, values_path
                    )
                except LatexTemplateError as e:
                    raise click.ClickException(f"Value organization failed: {e}")
                status_secho(
                    f"🧩 Organized {value_count} editable values into "
                    f"{template_path} and {values_path}",
                    fg="green",
                )
        else:
            write_output(None, result, "Successfully converted!")

    except click.ClickException:
        raise
    except Exception as e:
        handle_cli_error(e, verbose)


@app.command("latex-template")
@click.option(
    "--input", "input_path", "-i", required=True, type=click.Path(exists=True)
)
@click.option("--output", "template_path", "-o", required=True, type=click.Path())
@click.option("--values-output", required=True, type=click.Path())
def latex_template(input_path, template_path, values_output):
    """Organize annotated LaTeX values into a template and editable JSON."""
    source = Path(input_path).expanduser().resolve()
    template = Path(template_path).expanduser().resolve()
    values = Path(values_output).expanduser().resolve()
    template.parent.mkdir(parents=True, exist_ok=True)
    values.parent.mkdir(parents=True, exist_ok=True)
    try:
        count = organize_latex_file(source, template, values)
    except LatexTemplateError as e:
        raise click.ClickException(str(e))
    status_secho(
        f"✅ Organized {count} values. Template: {template}; values: {values}",
        fg="green",
    )


@app.command("latex-fill")
@click.option(
    "--template", "template_path", required=True, type=click.Path(exists=True)
)
@click.option("--values", "values_path", required=True, type=click.Path(exists=True))
@click.option("--output", "output_path", "-o", required=True, type=click.Path())
def latex_fill(template_path, values_path, output_path):
    """Fill a Lexoid LaTeX template from an edited values JSON file."""
    template = Path(template_path).expanduser().resolve()
    values = Path(values_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        fill_latex_template_file(template, values, output)
    except (LatexTemplateError, json.JSONDecodeError) as e:
        raise click.ClickException(str(e))
    status_secho(f"✅ Filled LaTeX saved to: {output}", fg="green")


if __name__ == "__main__":
    app()
