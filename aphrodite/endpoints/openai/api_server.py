import asyncio
import copy
import importlib
import inspect
import json
import multiprocessing
import os
import re
import signal
import tempfile
from argparse import Namespace
from contextlib import asynccontextmanager
from distutils.util import strtobool
from functools import partial
from http import HTTPStatus
from typing import (Any, AsyncGenerator, AsyncIterator, Dict, List, Optional,
                    Set, Tuple)

import yaml
from fastapi import APIRouter, FastAPI, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (HTMLResponse, JSONResponse, Response,
                               StreamingResponse)
from loguru import logger
from starlette.datastructures import State
from starlette.routing import Mount

import aphrodite.common.envs as envs
from aphrodite.common.config import ModelConfig
from aphrodite.common.outputs import RequestOutput
from aphrodite.common.sampling_params import _SAMPLING_EPS, SamplingParams
from aphrodite.common.utils import (FlexibleArgumentParser,
                                    get_open_zmq_ipc_path, in_windows,
                                    random_uuid)
from aphrodite.endpoints.logger import RequestLogger
from aphrodite.endpoints.openai.args import make_arg_parser
# yapf: disable
from aphrodite.endpoints.openai.protocol import (ChatCompletionRequest,
                                                 ChatCompletionResponse,
                                                 CompletionRequest,
                                                 DetokenizeRequest,
                                                 DetokenizeResponse,
                                                 EmbeddingRequest,
                                                 ErrorResponse,
                                                 KAIGenerationInputSchema,
                                                 TokenizeRequest,
                                                 TokenizeResponse)
from aphrodite.endpoints.openai.rpc.client import AsyncEngineRPCClient
from aphrodite.endpoints.openai.rpc.server import run_rpc_server
# yapf: enable
from aphrodite.endpoints.openai.serving_chat import OpenAIServingChat
from aphrodite.endpoints.openai.serving_completions import (
    OpenAIServingCompletion)
from aphrodite.endpoints.openai.serving_embedding import OpenAIServingEmbedding
from aphrodite.endpoints.openai.serving_engine import (LoRAModulePath,
                                                       PromptAdapterPath)
from aphrodite.endpoints.openai.serving_tokenization import (
    OpenAIServingTokenization)
from aphrodite.engine.args_tools import AsyncEngineArgs
from aphrodite.engine.async_aphrodite import AsyncAphrodite
from aphrodite.engine.protocol import AsyncEngineClient
from aphrodite.modeling.model_loader.weight_utils import get_model_config_yaml
from aphrodite.server import serve_http
from aphrodite.transformers_utils.tokenizer import get_tokenizer
from aphrodite.version import __version__ as APHRODITE_VERSION

if in_windows():
    import winloop as uvloop
else:
    import uvloop

TIMEOUT_KEEP_ALIVE = 5  # seconds
SERVE_KOBOLD_LITE_UI = strtobool(os.getenv("SERVE_KOBOLD_LITE_UI", "1"))

router = APIRouter()
kai_api = APIRouter()
extra_api = APIRouter()
kobold_lite_ui = ""
sampler_json = ""
gen_cache: dict = {}
prometheus_multiproc_dir: tempfile.TemporaryDirectory
model_is_loaded = True

_running_tasks: Set[asyncio.Task] = set()


def model_is_embedding(model_name: str, trust_remote_code: bool,
                       quantization: Optional[str]) -> bool:
    return ModelConfig(model=model_name,
                       tokenizer=model_name,
                       tokenizer_mode="auto",
                       trust_remote_code=trust_remote_code,
                       quantization=quantization,
                       seed=0,
                       dtype="auto").embedding_mode


@asynccontextmanager
async def lifespan(app: FastAPI):

    try:
        if app.state.log_stats:
            async_engine_client = app.state.engine_client
            async def _force_log():
                while True:
                    await asyncio.sleep(10)
                    await async_engine_client.do_log_stats()
            task = asyncio.create_task(_force_log())
            _running_tasks.add(task)
            task.add_done_callback(_running_tasks.remove)
        else:
            task = None
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
    finally:
        # Ensure app state including engine ref is gc'd
        del app.state


@asynccontextmanager
async def build_async_engine_client(
        args: Namespace) -> AsyncIterator[Optional[AsyncEngineClient]]:

    # Context manager to handle async_engine_client lifecycle
    # Ensures everything is shutdown and cleaned up on error/exit
    engine_args = AsyncEngineArgs.from_cli_args(args)

    async with build_async_engine_client_from_engine_args(
            engine_args, args.disable_frontend_multiprocessing) as engine:

        yield engine


@asynccontextmanager
async def build_async_engine_client_from_engine_args(
    engine_args: AsyncEngineArgs,
    disable_frontend_multiprocessing: bool = False,
) -> AsyncIterator[Optional[AsyncEngineClient]]:
    """
    Create AsyncEngineClient, either:
        - in-process using the AsyncAphrodite Directly
        - multiprocess using AsyncAphrodite RPC

    Returns the Client or None if the creation failed.
    """

    # If manually triggered or embedding model, use AsyncAphrodite in process.
    # TODO: support embedding model via RPC.
    if (model_is_embedding(engine_args.model, engine_args.trust_remote_code,
                           engine_args.quantization)
            or disable_frontend_multiprocessing):
        engine_config = engine_args.create_engine_config()
        uses_ray = getattr(AsyncAphrodite._get_executor_cls(engine_config),
                           "uses_ray", False)
        build_engine = partial(AsyncAphrodite.from_engine_args,
                               engine_args=engine_args,
                               engine_config=engine_config)
        if uses_ray:
            # Must run in main thread with ray for its signal handlers to work
            engine_client = build_engine()
        else:
            engine_client = await asyncio.get_running_loop().run_in_executor(
                None, build_engine)
        yield engine_client
        return

    # Otherwise, use the multiprocessing AsyncAphrodite.
    else:
        if "PROMETHEUS_MULTIPROC_DIR" not in os.environ:
            # Make TemporaryDirectory for prometheus multiprocessing
            # Note: global TemporaryDirectory will be automatically
            #   cleaned up upon exit.
            global prometheus_multiproc_dir
            prometheus_multiproc_dir = tempfile.TemporaryDirectory()
            os.environ[
                "PROMETHEUS_MULTIPROC_DIR"] = prometheus_multiproc_dir.name
        else:
            logger.warning(
                "Found PROMETHEUS_MULTIPROC_DIR was set by user. "
                "This directory must be wiped between Aphrodite runs or "
                "you will find inaccurate metrics. Unset the variable "
                "and Aphrodite will properly handle cleanup.")

        # Select random path for IPC.
        rpc_path = get_open_zmq_ipc_path()
        logger.info(f"Multiprocessing frontend to use {rpc_path} for RPC Path."
                    )

        # Build RPCClient, which conforms to AsyncEngineClient Protocol.
        # NOTE: Actually, this is not true yet. We still need to support
        # embedding models via RPC (see TODO above)
        rpc_client = AsyncEngineRPCClient(rpc_path)

        # Start RPCServer in separate process (holds the AsyncAphrodite).
        context = multiprocessing.get_context("spawn")
        # the current process might have CUDA context,
        # so we need to spawn a new process
        rpc_server_process = context.Process(
            target=run_rpc_server,
            args=(engine_args, rpc_path))
        rpc_server_process.start()
        logger.info(
            f"Started engine process with PID {rpc_server_process.pid}")

        try:
            while True:
                try:
                    await rpc_client.setup()
                    break
                except TimeoutError:
                    if not rpc_server_process.is_alive():
                        logger.error(
                            "RPCServer process died before responding "
                            "to readiness probe")
                        yield None
                        return

            yield rpc_client  # type: ignore[misc]
        finally:
            # Ensure rpc server process was terminated
            rpc_server_process.terminate()

            # Close all open connections to the backend
            rpc_client.close()

            # Wait for server process to join
            rpc_server_process.join()

            # Lazy import for prometheus multiprocessing.
            # We need to set PROMETHEUS_MULTIPROC_DIR environment variable
            # before prometheus_client is imported.
            # See https://prometheus.github.io/client_python/multiprocess/
            from prometheus_client import multiprocess
            multiprocess.mark_process_dead(rpc_server_process.pid)


async def _maybe_switch_model(
        request_model: str, app_state,
        raw_request: Request) -> Optional[ErrorResponse]:
    """Switch to requested model if different from currently loaded one."""
    global model_is_loaded, async_engine_client, engine_args, served_model_names
    
    if not model_is_loaded:
        return None

    models = await completion(raw_request).show_available_models()

    for model in models.data:
        if request_model in (model.id, model.root):
            return None

    if not app_state.args.allow_inline_model_loading:
        return JSONResponse(
            content={
                "error": {
                    "message": "Requested model is not currently loaded. "
                    "Inline model loading is disabled. Enable it with "
                    "--allow-inline-model-loading.",
                    "type": "invalid_request_error",
                    "code": "model_not_loaded"
                }
            },
            status_code=400
        )  # type: ignore

    # Authentication checks
    api_key = envs.APHRODITE_API_KEY or app_state.args.api_keys
    admin_key = envs.APHRODITE_ADMIN_KEY or app_state.args.admin_key

    if api_key:
        api_key_header = raw_request.headers.get("x-api-key")
        auth_header = raw_request.headers.get("Authorization")

        if not admin_key:
            return JSONResponse(
                content={
                    "error": {
                        "message": "Admin key not configured. "
                        "Inline model loading is disabled.",
                        "type": "invalid_request_error",
                        "code": "admin_key_required"
                    }
                },
                status_code=401
            )  # type: ignore

        if not (api_key_header == admin_key or
                auth_header == f"Bearer {admin_key}"):
            return JSONResponse(
                content={
                    "error": {
                        "message": "Admin privileges required for inline "
                        "model loading.",
                        "type": "invalid_request_error",
                        "code": "unauthorized"
                    }
                },
                status_code=401
            )  # type: ignore
    
    logger.info(f"Switching from {served_model_names[0]} to {request_model}")
    
    try:
        args = app_state.args
        current_client = engine_client(raw_request)
        
        # First shut down the current engine
        if not args.disable_frontend_multiprocessing:
            await current_client.kill()
        else:
            await current_client.shutdown_background_loop()
            
        model_is_loaded = False

        yaml_config = get_model_config_yaml(request_model, args.download_dir)

        if yaml_config:
            parser = FlexibleArgumentParser()
            parser = make_arg_parser(parser)
            engine_args = parser.parse_args([])  # empty args

            for key, value in yaml_config.items():
                if hasattr(engine_args, key):
                    setattr(engine_args, key, value)

            engine_args.model = request_model
            engine_args = AsyncEngineArgs.from_cli_args(engine_args)
        else:
            # Fallback to minimal config
            engine_args = AsyncEngineArgs(model=request_model)

        # Create new engine client without context manager
        if (model_is_embedding(engine_args.model, engine_args.trust_remote_code,
                             engine_args.quantization)
                or args.disable_frontend_multiprocessing):
            new_engine_client = AsyncAphrodite.from_engine_args(engine_args)
            await new_engine_client.setup()
        else:
            if "PROMETHEUS_MULTIPROC_DIR" not in os.environ:
                global prometheus_multiproc_dir
                prometheus_multiproc_dir = tempfile.TemporaryDirectory()
                os.environ[
                    "PROMETHEUS_MULTIPROC_DIR"] = prometheus_multiproc_dir.name

            rpc_path = get_open_zmq_ipc_path()
            logger.info(
                f"Multiprocessing frontend to use {rpc_path} for RPC Path.")

            rpc_client = AsyncEngineRPCClient(rpc_path)

            context = multiprocessing.get_context("spawn")
            rpc_server_process = context.Process(
                target=run_rpc_server,
                args=(engine_args, rpc_path))
            rpc_server_process.start()
            logger.info(
                f"Started engine process with PID {rpc_server_process.pid}")

            try:
                while True:
                    try:
                        await rpc_client.setup()
                        break
                    except TimeoutError as e:
                        if not rpc_server_process.is_alive():
                            raise RuntimeError(
                                "RPC Server died before responding to "
                                "readiness probe") from e
                
                new_engine_client = rpc_client
                model_config = await new_engine_client.get_model_config()
                new_args = copy.deepcopy(args)
                new_args.model = request_model
                
                init_app_state(
                    new_engine_client, model_config,
                    raw_request.app.state, new_args)
                
                served_model_names = [request_model]
                model_is_loaded = True
                return None

            except Exception as e:
                # Clean up RPC resources on error
                rpc_server_process.terminate()
                rpc_client.close()
                rpc_server_process.join()
                raise e

    except Exception as e:
        error_msg = f"Error while switching models: {str(e)}"
        logger.error(error_msg)
        return JSONResponse(
            content={
                "error": {
                    "message": error_msg,
                    "type": "invalid_request_error",
                    "code": "model_load_error"
                }
            },
            status_code=500
        )  # type: ignore

def mount_metrics(app: FastAPI):
    # Lazy import for prometheus multiprocessing.
    # We need to set PROMETHEUS_MULTIPROC_DIR environment variable
    # before prometheus_client is imported.
    # See https://prometheus.github.io/client_python/multiprocess/
    from prometheus_client import (CollectorRegistry, make_asgi_app,
                                   multiprocess)
    prometheus_multiproc_dir_path = os.getenv("PROMETHEUS_MULTIPROC_DIR", None)
    if prometheus_multiproc_dir_path is not None:
        logger.info(f"Aphrodite to use {prometheus_multiproc_dir_path} "
                    "as PROMETHEUS_MULTIPROC_DIR")
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        # Add prometheus asgi middleware to route /metrics requests
        metrics_route = Mount("/metrics", make_asgi_app(registry=registry))
    else:
        # Add prometheus asgi middleware to route /metrics requests
        metrics_route = Mount("/metrics", make_asgi_app())
    # Workaround for 307 Redirect for /metrics
    metrics_route.path_regex = re.compile('^/metrics(?P<path>.*)$')
    app.routes.append(metrics_route)


def chat(request: Request) -> OpenAIServingChat:
    return request.app.state.openai_serving_chat

def completion(request: Request) -> OpenAIServingCompletion:
    return request.app.state.openai_serving_completion

def tokenization(request: Request) -> OpenAIServingTokenization:
    return request.app.state.openai_serving_tokenization

def embedding(request: Request) -> OpenAIServingEmbedding:
    return request.app.state.openai_serving_embedding

def engine_client(request: Request) -> AsyncEngineClient:
    return request.app.state.engine_client


@router.delete("/v1/model/unload")
async def unload_model(raw_request: Request):
    """Unload the current model and shut down the server."""
    logger.info("Received request to unload model.")

    try:
        args = raw_request.app.state.args
        if not args.disable_frontend_multiprocessing:
            await engine_client(raw_request).kill()
        else:
            await engine_client(raw_request).shutdown_background_loop()

        global model_is_loaded
        model_is_loaded = False
        return JSONResponse(
            content={
                "status": "success",
                "message": "Model unloaded successfully"
            }
        )

    except Exception as e:
        error_msg = f"Error while unloading model: {str(e)}"
        logger.error(error_msg)
        return JSONResponse(
            content={"status": "error", "message": error_msg},
            status_code=500
        )


@router.post("/v1/model/load")
async def load_model(config_file: UploadFile, raw_request: Request):
    """Load a model using a YAML configuration file."""
    global model_is_loaded, async_engine_client, engine_args

    if model_is_loaded:
        return JSONResponse(
            content={
                "error": {
                    "message": "A model is already loaded. "
                    "Please unload it first.",
                    "type": "invalid_request_error",
                    "code": "model_already_loaded"
                }
            },
            status_code=400
        )

    try:
        config_text = await config_file.read()
        config: Dict[Any, Any] = yaml.safe_load(config_text)

        args = []
        for key, value in config.items():
            key = key.replace('_', '-')

            if isinstance(value, bool):
                if value:
                    args.append(f"--{key}")
            elif isinstance(value, (list, tuple)):
                if key in ['lora-modules', 'prompt-adapters']:
                    for item in value:
                        args.append(f"--{key}")
                        args.append(f"{item['name']}={item['path']}")
                else:
                    for item in value:
                        args.append(f"--{key}")
                        args.append(str(item))
            else:
                args.append(f"--{key}")
                args.append(str(value))

        parser = FlexibleArgumentParser()
        parser = make_arg_parser(parser)
        parsed_args = parser.parse_args(args)
        engine_args = AsyncEngineArgs.from_cli_args(parsed_args)

        # Create new engine client without context manager
        if (model_is_embedding(engine_args.model,
                               engine_args.trust_remote_code, 
                               engine_args.quantization)
                or parsed_args.disable_frontend_multiprocessing):
            new_engine_client = AsyncAphrodite.from_engine_args(engine_args)
            await new_engine_client.setup()
        else:
            if "PROMETHEUS_MULTIPROC_DIR" not in os.environ:
                global prometheus_multiproc_dir
                prometheus_multiproc_dir = tempfile.TemporaryDirectory()
                os.environ[
                    "PROMETHEUS_MULTIPROC_DIR"] = prometheus_multiproc_dir.name

            rpc_path = get_open_zmq_ipc_path()
            logger.info(
                f"Multiprocessing frontend to use {rpc_path} for RPC Path.")

            rpc_client = AsyncEngineRPCClient(rpc_path)
            new_engine_client = rpc_client

            context = multiprocessing.get_context("spawn")
            rpc_server_process = context.Process(
                target=run_rpc_server,
                args=(engine_args, rpc_path))
            rpc_server_process.start()
            logger.info(
                f"Started engine process with PID {rpc_server_process.pid}")

            while True:
                try:
                    await new_engine_client.setup()
                    break
                except TimeoutError as e:
                    if not rpc_server_process.is_alive():
                        raise RuntimeError(
                            "RPC Server died before responding to readiness "
                            "probe") from e

        model_config = await engine_client(raw_request).get_model_config()
        init_app_state(engine_client(raw_request), model_config,
                       raw_request.app.state, parsed_args)
        
        model_is_loaded = True
        return JSONResponse(
            content={
                "status": "success",
                "message": "Model loaded successfully"
            }
        )

    except Exception as e:
        error_msg = f"Error while loading model: {str(e)}"
        logger.error(error_msg)
        return JSONResponse(
            content={
                "error": {
                    "message": error_msg,
                    "type": "invalid_request_error",
                    "code": "model_load_error"
                }
            },
            status_code=500
        )

@router.get("/health")
async def health(raw_request: Request) -> Response:
    """Health check."""
    await engine_client(raw_request).check_health()
    return Response(status_code=200)


@router.post("/v1/tokenize")
async def tokenize(request: TokenizeRequest, raw_request: Request):
    if not model_is_loaded:
        return JSONResponse(
            content={
                "status": "error",
                "message": "No model loaded."
            },
            status_code=500
        )
    generator = await tokenization(raw_request).create_tokenize(request)
    if isinstance(generator, ErrorResponse):
        return JSONResponse(content=generator.model_dump(),
                            status_code=generator.code)
    else:
        assert isinstance(generator, TokenizeResponse)
        return JSONResponse(content=generator.model_dump())


@router.post("/v1/detokenize")
async def detokenize(request: DetokenizeRequest, raw_request: Request):
    if not model_is_loaded:
        return JSONResponse(
            content={
                "status": "error",
                "message": "No model loaded."
            },
            status_code=500
        )
    generator = await tokenization(raw_request).create_detokenize(request)
    if isinstance(generator, ErrorResponse):
        return JSONResponse(content=generator.model_dump(),
                            status_code=generator.code)
    else:
        assert isinstance(generator, DetokenizeResponse)
        return JSONResponse(content=generator.model_dump())


@router.get("/v1/models")
async def show_available_models(raw_request: Request):
    models = await completion(raw_request).show_available_models()
    return JSONResponse(content=models.model_dump())


@router.get("/version")
async def show_version():
    ver = {"version": APHRODITE_VERSION}
    return JSONResponse(content=ver)


@router.get("/.well-known/serviceinfo")
async def serviceinfo():
    """Return service information including version, API endpoints,
    and documentation URLs."""
    
    return JSONResponse(content={
        "version": 0.2,
        "software": {
            "name": "Aphrodite Engine",
            "version": APHRODITE_VERSION,
            "repository": "https://github.com/PygmalionAI/aphrodite-engine",
            "homepage": "https://aphrodite.pygmalion.chat",
            "logo": "https://pygmalion.chat/icons/favicon.ico",
        },
        "api": {
            "openai": {
                "name": "OpenAI API",
                "rel_url": "/v1",
                "documentation": "/redoc",
                "version": 1,
            },
            "koboldai": {
                "name": "KoboldAI API", 
                "rel_url": "/api",
                "documentation": "/redoc",
                "version": 1,
            }
        }
    })


@router.post("/v1/chat/completions")
async def create_chat_completion(request: ChatCompletionRequest,
                                 raw_request: Request):
    error_check_ret = await _maybe_switch_model(
        request.model, raw_request.app.state, raw_request)
    if error_check_ret is not None:
        return error_check_ret
    generator = await chat(raw_request).create_chat_completion(
        request, raw_request)
    if isinstance(generator, ErrorResponse):
        return JSONResponse(content=generator.model_dump(),
                            status_code=generator.code)
    if request.stream:
        return StreamingResponse(content=generator,
                                 media_type="text/event-stream")
    else:
        assert isinstance(generator, ChatCompletionResponse)
        return JSONResponse(content=generator.model_dump())


@router.post("/v1/completions")
async def create_completion(request: CompletionRequest, raw_request: Request):
    error_check_ret = await _maybe_switch_model(
        request.model, raw_request.app.state, raw_request)
    if error_check_ret is not None:
        return error_check_ret
    generator = await completion(raw_request).create_completion(
        request, raw_request)
    if isinstance(generator, ErrorResponse):
        return JSONResponse(content=generator.model_dump(),
                            status_code=generator.code)
    if request.stream:
        return StreamingResponse(content=generator,
                                 media_type="text/event-stream")
    else:
        return JSONResponse(content=generator.model_dump())


@router.post("/v1/embeddings")
async def create_embedding(request: EmbeddingRequest, raw_request: Request):
    if not model_is_loaded:
        return JSONResponse(
            content={
                "status": "error",
                "message": "No model loaded."
            },
            status_code=500
        )
    generator = await embedding(raw_request).create_embedding(
        request, raw_request)
    if isinstance(generator, ErrorResponse):
        return JSONResponse(content=generator.model_dump(),
                            status_code=generator.code)
    else:
        return JSONResponse(content=generator.model_dump())
    

@router.post("/v1/lora/load")
async def load_lora(lora: LoRAModulePath, raw_request: Request):
    if not model_is_loaded:
        return JSONResponse(
            content={
                "status": "error",
                "message": "No model loaded."
            },
            status_code=500
        )
    completion(raw_request).add_lora(lora)
    if engine_args.enable_lora is False:
        logger.error("LoRA is not enabled in the engine. "
                     "Please start the server with the "
                     "--enable-lora flag!")
    return JSONResponse(content={"status": "success"})


@router.delete("/v1/lora/unload")
async def unload_lora(lora_name: str, raw_request: Request):
    if not model_is_loaded:
        return JSONResponse(
            content={
                "status": "error",
                "message": "No model loaded."
            },
            status_code=500
        )
    completion(raw_request).remove_lora(lora_name)
    return JSONResponse(content={"status": "success"})


@router.post("/v1/soft_prompt/load")
async def load_soft_prompt(soft_prompt: PromptAdapterPath,
                           raw_request: Request):
    if not model_is_loaded:
        return JSONResponse(
            content={
                "status": "error",
                "message": "No model loaded."
            },
            status_code=500
        )
    completion(raw_request).add_prompt_adapter(soft_prompt)
    if engine_args.enable_prompt_adapter is False:
        logger.error("Prompt Adapter is not enabled in the engine. "
                     "Please start the server with the "
                     "--enable-prompt-adapter flag!")
    return JSONResponse(content={"status": "success"})

@router.delete("/v1/soft_prompt/unload")
async def unload_soft_prompt(soft_prompt_name: str, raw_request: Request):
    if not model_is_loaded:
        return JSONResponse(
            content={
                "status": "error",
                "message": "No model loaded."
            },
            status_code=500
        )
    completion(raw_request).remove_prompt_adapter(soft_prompt_name)
    return JSONResponse(content={"status": "success"})


# ============ KoboldAI API ============ #

badwordsids: List[int] = []

def _set_badwords(tokenizer, hf_config):  # pylint: disable=redefined-outer-name
    # pylint: disable=global-variable-undefined
    global badwordsids
    if hf_config.bad_words_ids is not None:
        badwordsids = hf_config.bad_words_ids
        return

    badwordsids = [
        v for k, v in tokenizer.get_vocab().items()
        if any(c in str(k) for c in "[]")
    ]
    if tokenizer.pad_token_id in badwordsids:
        badwordsids.remove(tokenizer.pad_token_id)
    badwordsids.append(tokenizer.eos_token_id)


def prepare_engine_payload(
        kai_payload: KAIGenerationInputSchema
) -> Tuple[SamplingParams, List[int]]:
    """Create SamplingParams and truncated input tokens for AsyncEngine"""

    if not kai_payload.genkey:
        kai_payload.genkey = f"kai-{random_uuid()}"

    kai_payload.top_k = kai_payload.top_k if kai_payload.top_k != 0.0 else -1
    kai_payload.tfs = max(_SAMPLING_EPS, kai_payload.tfs)
    if kai_payload.temperature < _SAMPLING_EPS:
        kai_payload.n = 1
        kai_payload.top_p = 1.0
        kai_payload.top_k = -1


    sampling_params = SamplingParams(
        n=kai_payload.n,
        best_of=kai_payload.n,
        repetition_penalty=kai_payload.rep_pen,
        temperature=kai_payload.temperature,
        smoothing_factor=kai_payload.smoothing_factor,
        smoothing_curve=kai_payload.smoothing_curve,
        tfs=kai_payload.tfs,
        top_p=kai_payload.top_p,
        top_k=kai_payload.top_k,
        top_a=kai_payload.top_a,
        min_p=kai_payload.min_p,
        typical_p=kai_payload.typical,
        eta_cutoff=kai_payload.eta_cutoff,
        epsilon_cutoff=kai_payload.eps_cutoff,
        stop=kai_payload.stop_sequence,
        include_stop_str_in_output=kai_payload.include_stop_str_in_output,
        custom_token_bans=badwordsids
        if kai_payload.use_default_badwordsids else [],
        max_tokens=kai_payload.max_length,
        seed=kai_payload.sampler_seed,
        xtc_probability=kai_payload.xtc_probability,
        xtc_threshold=kai_payload.xtc_threshold,
    )

    max_input_tokens = max(
        1, kai_payload.max_context_length - kai_payload.max_length)
    input_tokens = tokenizer(kai_payload.prompt).input_ids[-max_input_tokens:]

    return sampling_params, input_tokens


@kai_api.post("/generate")
async def generate(kai_payload: KAIGenerationInputSchema,
                   raw_request: Request) -> JSONResponse:
    sampling_params, input_tokens = prepare_engine_payload(kai_payload)
    result_generator = engine_client(raw_request).generate(
        {
            "prompt": kai_payload.prompt,
            "prompt_token_ids": input_tokens,
        },
        sampling_params,
        kai_payload.genkey,
    )

    final_res: RequestOutput = None
    previous_output = ""
    async for res in result_generator:
        final_res = res
        new_chunk = res.outputs[0].text[len(previous_output):]
        previous_output += new_chunk
        gen_cache[kai_payload.genkey] = previous_output

    assert final_res is not None
    del gen_cache[kai_payload.genkey]

    return JSONResponse(
        {"results": [{
            "text": output.text
        } for output in final_res.outputs]})


@extra_api.post("/generate/stream")
async def generate_stream(
        kai_payload: KAIGenerationInputSchema,
        raw_request: Request) -> StreamingResponse:

    sampling_params, input_tokens = prepare_engine_payload(kai_payload)
    results_generator = engine_client(raw_request).generate(
        {
            "prompt": kai_payload.prompt,
            "prompt_token_ids": input_tokens,
        },
        sampling_params,
        kai_payload.genkey,
    )

    async def stream_kobold() -> AsyncGenerator[bytes, None]:
        previous_output = ""
        async for res in results_generator:
            new_chunk = res.outputs[0].text[len(previous_output):]
            previous_output += new_chunk
            yield b"event: message\n"
            yield f"data: {json.dumps({'token': new_chunk})}\n\n".encode()

    return StreamingResponse(stream_kobold(),
                             headers={
                                 "Cache-Control": "no-cache",
                                 "Connection": "keep-alive",
                             },
                             media_type="text/event-stream")


@extra_api.post("/generate/check")
@extra_api.get("/generate/check")
async def check_generation(request: Request):
    text = ""
    try:
        request_dict = await request.json()
        if "genkey" in request_dict and request_dict["genkey"] in gen_cache:
            text = gen_cache[request_dict["genkey"]]
    except json.JSONDecodeError:
        pass

    return JSONResponse({"results": [{"text": text}]})


@extra_api.post("/abort")
async def abort_generation(raw_request: Request):
    try:
        request_dict = await raw_request.json()
        if "genkey" in request_dict:
            await engine_client(raw_request).abort(request_dict["genkey"])
    except json.JSONDecodeError:
        pass

    return JSONResponse({})


@extra_api.post("/tokencount")
async def count_tokens(request: TokenizeRequest, raw_request: Request):
    """Tokenize string and return token count"""

    generator = await tokenization(raw_request).create_tokenize(request)
    return JSONResponse({"value": generator.model_dump()["tokens"]})


@kai_api.get("/info/version")
async def get_version():
    """Impersonate KAI"""
    return JSONResponse({"result": "1.2.4"})


@kai_api.get("/model")
async def get_model():
    return JSONResponse({"result": f"aphrodite/{served_model_names[0]}"})


@kai_api.get("/config/soft_prompts_list")
async def get_available_softprompts():
    """Stub for compatibility"""
    return JSONResponse({"values": []})


@kai_api.get("/config/soft_prompt")
async def get_current_softprompt():
    """Stub for compatibility"""
    return JSONResponse({"value": ""})


@kai_api.put("/config/soft_prompt")
async def set_current_softprompt():
    """Stub for compatibility"""
    return JSONResponse({})


@kai_api.get("/config/max_length")
async def get_max_length() -> JSONResponse:
    max_length = args.max_length
    return JSONResponse({"value": max_length})


@kai_api.get("/config/max_context_length")
@extra_api.get("/true_max_context_length")
async def get_max_context_length() -> JSONResponse:
    max_context_length = engine_args.max_model_len
    return JSONResponse({"value": max_context_length})


@extra_api.get("/preloadstory")
async def get_preloaded_story() -> JSONResponse:
    """Stub for compatibility"""
    return JSONResponse({})


@extra_api.get("/version")
async def get_extra_version():
    """Impersonate KoboldCpp"""
    return JSONResponse({"result": "KoboldCpp", "version": "1.63"})


@router.get("/")
async def get_kobold_lite_ui():
    """Serves a cached copy of the Kobold Lite UI, loading it from disk
    on demand if needed. Can be disabled with SERVE_KOBOLD_LITE_UI=0."""
    if not SERVE_KOBOLD_LITE_UI:
        return JSONResponse(content={"error": "Kobold Lite UI is disabled"},
                            status_code=404)
    global kobold_lite_ui
    if kobold_lite_ui == "":
        scriptpath = os.path.dirname(os.path.abspath(__file__))
        klitepath = os.path.join(scriptpath, "../kobold/klite.embd")
        klitepath = os.path.normpath(klitepath)  # Normalize the path
        if os.path.exists(klitepath):
            with open(klitepath, "r", encoding="utf-8") as f:
                kobold_lite_ui = f.read()
        else:
            logger.error("Kobold Lite UI not found at " + klitepath)
    return HTMLResponse(content=kobold_lite_ui)


# ============ KoboldAI API ============ #


def build_app(args: Namespace) -> FastAPI:
    app = FastAPI(lifespan=lifespan)
    app.include_router(router)
    app.root_path = args.root_path
    app.state.args = args
    if args.launch_kobold_api:
        logger.warning("Kobold API is now enabled by default. "
                       "This flag will be removed in the future.")
    app.include_router(kai_api, prefix="/api/v1")
    app.include_router(kai_api,
                        prefix="/api/latest",
                        include_in_schema=False)
    app.include_router(extra_api, prefix="/api/extra")

    mount_metrics(app)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=args.allowed_origins,
        allow_credentials=args.allow_credentials,
        allow_methods=args.allowed_methods,
        allow_headers=args.allowed_headers,
    )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(_, exc):
        chat = app.state.openai_serving_chat
        err = chat.create_error_response(message=str(exc))
        return JSONResponse(err.model_dump(),
                            status_code=HTTPStatus.BAD_REQUEST)

    if token := envs.APHRODITE_API_KEY or args.api_keys:
        admin_key = os.environ.get("APHRODITE_ADMIN_KEY") or args.admin_key

        if admin_key is None:
            logger.warning("Admin key not provided. Admin operations will "
                           "be disabled.")

        @app.middleware("http")
        async def authentication(request: Request, call_next):

            if not request.url.path.startswith(("/v1", "/api")):
                return await call_next(request)

            # Browsers may send OPTIONS requests to check CORS headers
            # before sending the actual request. We should allow these
            # requests to pass through without authentication.
            # See https://github.com/PygmalionAI/aphrodite-engine/issues/434
            if request.method == "OPTIONS":
                return await call_next(request)

            auth_header = request.headers.get("Authorization")
            api_key_header = request.headers.get("x-api-key")

            if request.url.path.startswith(
                ("/v1/lora", "/v1/soft_prompt", "/v1/model")):
                if admin_key is not None and (
                    api_key_header == admin_key or 
                    auth_header == "Bearer " + admin_key
                ):
                    return await call_next(request)
                return JSONResponse(content={"error": "Unauthorized"},
                                    status_code=401)

            if (auth_header == f"Bearer {token}" or api_key_header == token or
                (admin_key is not None and
                 (api_key_header == admin_key or
                  auth_header == f"Bearer {admin_key}"))):
                return await call_next(request)

            return JSONResponse(
                content={"error": "Unauthorized"}, status_code=401)

    for middleware in args.middleware:
        module_path, object_name = middleware.rsplit(".", 1)
        imported = getattr(importlib.import_module(module_path), object_name)
        if inspect.isclass(imported):
            app.add_middleware(imported)
        elif inspect.iscoroutinefunction(imported):
            app.middleware("http")(imported)
        else:
            raise ValueError(f"Invalid middleware {middleware}. "
                             f"Must be a function or a class.")

    return app


def init_app_state(
    async_engine_client: AsyncEngineClient,
    model_config: ModelConfig,
    state: State,
    args: Namespace,
) -> None:
    global api_server_args
    api_server_args = args

    logger.debug(f"args: {args}")

    global served_model_names
    if args.served_model_name is not None:
        served_model_names = args.served_model_name
    else:
        served_model_names = [args.model]

    if args.uvloop:
        uvloop.install()

    global tokenizer

    if args.disable_log_requests:
        request_logger = None
    else:
        request_logger = RequestLogger(max_log_len=args.max_log_len)

    state.engine_client = async_engine_client
    state.log_stats = not args.disable_log_stats

    state.openai_serving_chat = OpenAIServingChat(
        async_engine_client,
        model_config,
        served_model_names,
        args.response_role,
        lora_modules=args.lora_modules,
        prompt_adapters=args.prompt_adapters,
        request_logger=request_logger,
        chat_template=args.chat_template,
        return_tokens_as_token_ids=args.return_tokens_as_token_ids,
        enable_auto_tools=args.enable_auto_tool_choice,
        tool_parser=args.tool_call_parser
    )
    state.openai_serving_completion = OpenAIServingCompletion(
        async_engine_client,
        model_config,
        served_model_names,
        lora_modules=args.lora_modules,
        prompt_adapters=args.prompt_adapters,
        request_logger=request_logger,
        return_tokens_as_token_ids=args.return_tokens_as_token_ids,
    )
    state.openai_serving_embedding = OpenAIServingEmbedding(
        async_engine_client,
        model_config,
        served_model_names,
        request_logger=request_logger,
    )
    state.openai_serving_tokenization = OpenAIServingTokenization(
        async_engine_client,
        model_config,
        served_model_names,
        lora_modules=args.lora_modules,
        request_logger=request_logger,
        chat_template=args.chat_template,
    )

    tokenizer = get_tokenizer(
        tokenizer_name=args.tokenizer if args.tokenizer else args.model,
        tokenizer_mode=args.tokenizer_mode,
        trust_remote_code=args.trust_remote_code,
        revision=args.revision,
    )

    if args.launch_kobold_api:
        _set_badwords(tokenizer, model_config.hf_config)
    

async def run_server(args, **uvicorn_kwargs) -> None:

    def signal_handler(*_) -> None:
        # Interrupt server on sigterm while initializing
        raise KeyboardInterrupt("terminated")
    signal.signal(signal.SIGTERM, signal_handler)

    async with build_async_engine_client(args) as async_engine_client:
        # If None, creation of the client failed and we exit.
        if async_engine_client is None:
            return
        app = build_app(args)
        model_config = await async_engine_client.get_model_config()
        init_app_state(async_engine_client, model_config, app.state, args)

        protocol = "https" if args.ssl_certfile else "http"
        root_path = args.root_path.rstrip("/") if args.root_path else ""
        host_name = args.host if args.host else "localhost"
        port_str = str(args.port)


        if SERVE_KOBOLD_LITE_UI:
            ui_url = f"{protocol}://{host_name}:{port_str}{root_path}/"
            logger.info(f"Kobold Lite UI:   {ui_url}")

        logger.info(f"Documentation:    {protocol}://{host_name}:{port_str}{root_path}/redoc")  # noqa: E501
        logger.info(f"Completions API:  {protocol}://{host_name}:{port_str}{root_path}/v1/completions")  # noqa: E501
        logger.info(f"Chat API:         {protocol}://{host_name}:{port_str}{root_path}/v1/chat/completions")  # noqa: E501
        logger.info(f"Embeddings API:   {protocol}://{host_name}:{port_str}{root_path}/v1/embeddings")  # noqa: E501
        logger.info(f"Tokenization API: {protocol}://{host_name}:{port_str}{root_path}/v1/tokenize")  # noqa: E501

        shutdown_task = await serve_http(
            app,
            limit_concurrency=async_engine_client.limit_concurrency,
            host=args.host,
            port=args.port,
            log_level=args.uvicorn_log_level,
            timeout_keep_alive=TIMEOUT_KEEP_ALIVE,
            ssl_keyfile=args.ssl_keyfile,
            ssl_certfile=args.ssl_certfile,
            ssl_ca_certs=args.ssl_ca_certs,
            ssl_cert_reqs=args.ssl_cert_reqs,
            **uvicorn_kwargs,
        )

    # NB: Await server shutdown only after the backend context is exited
    await shutdown_task


if __name__ == "__main__":
    # NOTE:
    # This section should be in sync with aphrodite/endpoints/cli.py
    # for CLI entrypoints.
    parser = FlexibleArgumentParser(
        description="Aphrodite OpenAI-Compatible RESTful API Server")
    parser = make_arg_parser(parser)
    args = parser.parse_args()

    uvloop.run(run_server(args))
