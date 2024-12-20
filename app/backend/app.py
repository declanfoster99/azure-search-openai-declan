import io
import json
import logging
import sys
import mimetypes
import os
import base64
import subprocess
from pathlib import Path
from typing import AsyncGenerator
import json

from azure.core.exceptions import ResourceNotFoundError
from azure.identity.aio import DefaultAzureCredential, get_bearer_token_provider
from azure.monitor.opentelemetry import configure_azure_monitor
from azure.search.documents.aio import SearchClient
from azure.storage.blob.aio import BlobServiceClient
from openai import APIError, AsyncAzureOpenAI, AsyncOpenAI
from opentelemetry.instrumentation.aiohttp_client import AioHttpClientInstrumentor
from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from quart import (
    Blueprint,
    Quart,
    abort,
    current_app,
    jsonify,
    make_response,
    request,
    send_file,
    send_from_directory,
)
from quart_cors import cors

from approaches.chatreadretrieveread import ChatReadRetrieveReadApproach
from approaches.retrievethenread import RetrieveThenReadApproach
from core.authentication import AuthenticationHelper

CONFIG_ASK_APPROACH = "ask_approach"
CONFIG_CHAT_APPROACH = "chat_approach"
CONFIG_BLOB_CONTAINER_CLIENT = "blob_container_client"
CONFIG_AUTH_CLIENT = "auth_client"
CONFIG_SEARCH_CLIENT = "search_client"
CONFIG_OPENAI_CLIENT = "openai_client"
ERROR_MESSAGE = """The app encountered an error processing your request.
If you are an administrator of the app, view the full error in the logs. See aka.ms/appservice-logs for more information.
Error type: {error_type}
"""
ERROR_MESSAGE_FILTER = """Your message contains content that was flagged by the OpenAI content filter."""

bp = Blueprint("routes", __name__, static_folder="static")
# Fix Windows registry issue with mimetypes
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")


@bp.route("/")
async def index():
    return await bp.send_static_file("index.html")


# Empty page is recommended for login redirect to work.
# See https://github.com/AzureAD/microsoft-authentication-library-for-js/blob/dev/lib/msal-browser/docs/initialization.md#redirecturi-considerations for more information
@bp.route("/redirect")
async def redirect():
    return ""


@bp.route("/favicon.ico")
async def favicon():
    return await bp.send_static_file("favicon.ico")


@bp.route("/assets/<path:path>")
async def assets(path):
    return await send_from_directory(Path(__file__).resolve().parent / "static" / "assets", path)


# Serve content files from blob storage from within the app to keep the example self-contained.
# *** NOTE *** this assumes that the content files are public, or at least that all users of the app
# can access all the files. This is also slow and memory hungry.
@bp.route("/content/<path>")
async def content_file(path: str):
    # Remove page number from path, filename-1.txt -> filename.txt
    if path.find("#page=") > 0:
        path_parts = path.rsplit("#page=", 1)
        path = path_parts[0]
    logging.info("Opening file %s at page %s", path)
    blob_container_client = current_app.config[CONFIG_BLOB_CONTAINER_CLIENT]
    try:
        blob = await blob_container_client.get_blob_client(path).download_blob()
    except ResourceNotFoundError:
        logging.exception("Path not found: %s", path)
        abort(404)
    if not blob.properties or not blob.properties.has_key("content_settings"):
        abort(404)
    mime_type = blob.properties["content_settings"]["content_type"]
    if mime_type == "application/octet-stream":
        mime_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
    blob_file = io.BytesIO()
    await blob.readinto(blob_file)
    blob_file.seek(0)
    return await send_file(blob_file, mimetype=mime_type, as_attachment=False, attachment_filename=path)


def error_dict(error: Exception) -> dict:
    if isinstance(error, APIError) and error.code == "content_filter":
        return {"error": ERROR_MESSAGE_FILTER}
    return {"error": ERROR_MESSAGE.format(error_type=type(error))}


def error_response(error: Exception, route: str, status_code: int = 500):
    logging.exception("Exception in %s: %s", route, error)
    if isinstance(error, APIError) and error.code == "content_filter":
        status_code = 400
    return jsonify(error_dict(error)), status_code


def run_prepdocs_script(index: str, container: str):
    script_path = os.path.join(os.path.dirname(
        __file__), 'scripts', 'prepdocs.sh')

    try:
        # Pass the index and container as arguments to the shell script
        result = subprocess.run(
            ['sh', script_path, index, container],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        print(f"Script output: {result.stdout.decode()}")
    except subprocess.CalledProcessError as e:
        print(f"Script failed with error: {e.stderr.decode()}")
        raise e


@bp.route("/ask", methods=["POST"])
async def ask():
    if not request.is_json:
        return jsonify({"error": "request must be json"}), 415
    request_json = await request.get_json()
    context = request_json.get("context", {})
    auth_helper = current_app.config[CONFIG_AUTH_CLIENT]
    context["auth_claims"] = await auth_helper.get_auth_claims_if_enabled(request.headers)
    try:
        approach = current_app.config[CONFIG_ASK_APPROACH]
        r = await approach.run(
            request_json["messages"], context=context, session_state=request_json.get(
                "session_state")
        )
        return jsonify(r)
    except Exception as error:
        return error_response(error, "/ask")


async def format_as_ndjson(r: AsyncGenerator[dict, None]) -> AsyncGenerator[str, None]:
    try:
        async for event in r:
            yield json.dumps(event, ensure_ascii=False) + "\n"
    except Exception as e:
        logging.exception("Exception while generating response stream: %s", e)
        yield json.dumps(error_dict(e))


@bp.route("/chat", methods=["POST"])
async def chat():

    if not request.is_json:
        return jsonify({"error": "request must be json"}), 415
    request_json = await request.get_json()

    await set_index_and_container(
        request_json["azureIndex"], request_json["azureContainer"])
    context = request_json.get("context", {})
    communicationFrameworkIndex = request_json["communicationFrameworkIndex"]
    toneIndex = request_json["toneIndex"]
    readabilityIndex = request_json["readabilityIndex"]
    wordCountIndex = request_json["wordCountIndex"]

    with open("prompts.json") as json_file:
        prompt_settings = json.load(json_file)

    initial_instructions = "I am sending a list of parameters alongside my query. The parameters are wrapped in curly braces and will describe the communication framework, tone, readability and wordcount of the response that is expected from you. Under no circumstances should you make any direct mention of these parameters in your response. My actual query will be appended at the very end, after all of the parameters. Some or all of these parameters may not exist, ignore this initial message if that is the case. "
    communication_framework_settings = ""
    tone_settings = ""
    readability_settings = ""
    wordcount_settings = ""

    if (communicationFrameworkIndex != 0):
        communication_framework_settings = "{communication_framework_settings: " + str(prompt_settings["communication_framework_settings"][str(
            communicationFrameworkIndex)]) + " } "
    if (toneIndex != 0):
        tone_settings = "{tone_settings: " + str(prompt_settings["tone_settings"][str(
            toneIndex)]) + " } "
    if (readabilityIndex != 0):
        readability_settings = "{readability_settings: " + str(prompt_settings["readability_settings"][str(
            readabilityIndex)]) + " } "
    if (wordCountIndex != 0):
        wordcount_settings = "{wordcount_settings: " + str(prompt_settings["wordcount_settings"][str(
            wordCountIndex)]) + " } "

    request_json["messages"][0]["content"] = initial_instructions + communication_framework_settings + tone_settings + \
        readability_settings + wordcount_settings + \
        request_json["messages"][0]["content"]

    auth_helper = current_app.config[CONFIG_AUTH_CLIENT]
    context["auth_claims"] = await auth_helper.get_auth_claims_if_enabled(request.headers)
    try:
        approach = current_app.config[CONFIG_CHAT_APPROACH]
        result = await approach.run(
            request_json["messages"],
            stream=request_json.get("stream", False),
            context=context,
            session_state=request_json.get("session_state"),
        )
        if isinstance(result, dict):
            return jsonify(result)
        else:
            response = await make_response(format_as_ndjson(result))
            response.timeout = None  # type: ignore
            response.mimetype = "application/json-lines"
            return response
    except Exception as error:
        return error_response(error, "/chat")


@bp.route("/runScript", methods=["POST"])
async def runScript():
    run_prepdocs_script()
    return jsonify({"result": "ranScript"})


@bp.route("/uploadFiles", methods=["POST"])
async def upload_files():
    # Get the current working directory
    current_directory = os.getcwd()
    print(f"Current working directory: {current_directory}")

    # Set the data folder path relative to the current directory
    DATA_FOLDER = os.path.join(current_directory, "data")
    print(f"Data folder path: {DATA_FOLDER}")

    # Ensure the data folder exists, create it if it doesn't
    if not os.path.exists(DATA_FOLDER):
        os.makedirs(DATA_FOLDER)
    else:
        # Delete all files in the data folder
        for filename in os.listdir(DATA_FOLDER):
            file_path = os.path.join(DATA_FOLDER, filename)
            try:
                os.unlink(file_path)  # Remove the file or symbolic link
            except Exception as e:
                print(f'Failed to delete {file_path}. Reason: {e}')

    if not request.is_json:
        return jsonify({"error": "Request must be JSON"}), 415

    request_json = await request.get_json()

    azure_index = request_json.get("azureIndex")
    azure_container = request_json.get("azureContainer")

    if not azure_index or not azure_container:
        return jsonify({"error": "azureIndex and azureContainer are required"}), 400

    files = request_json.get("files", [])

    if not files:
        return jsonify({"error": "No files provided"}), 400

    # Save the files to the data folder
    for file in files:
        file_name = file["name"]
        # Split to remove the metadata prefix
        file_content = file["content"].split(",")[1]

        # Decode the base64 content
        file_data = base64.b64decode(file_content)

        # Construct the file path using the relative data folder
        file_path = os.path.join(DATA_FOLDER, file_name)
        print(f"Saving file to: {file_path}")

        # Save the file to the data folder
        with open(file_path, "wb") as f:
            f.write(file_data)

    # Handle the index and container logic here
    await set_index_and_container(azure_index, azure_container)
    run_prepdocs_script(azure_index, azure_container)

    # Delete contents of the data folder
    for filename in os.listdir(DATA_FOLDER):
        file_path = os.path.join(DATA_FOLDER, filename)
        try:
            os.unlink(file_path)  # Remove the file or symbolic link
        except Exception as e:
            print(f'Failed to delete {file_path}. Reason: {e}')

    return jsonify({
        "result": "Files uploaded and processed successfully",
        "azureIndex": azure_index,
        "azureContainer": azure_container
    })


# Send MSAL.js settings to the client UI
@bp.route("/auth_setup", methods=["GET"])
def auth_setup():
    auth_helper = current_app.config[CONFIG_AUTH_CLIENT]
    return jsonify(auth_helper.get_auth_setup_for_client())


@bp.before_app_serving
async def setup_clients():
    # Replace these with your own values, either in environment variables or directly here
    AZURE_STORAGE_ACCOUNT = os.environ["AZURE_STORAGE_ACCOUNT"]
    AZURE_STORAGE_CONTAINER = os.environ["AZURE_STORAGE_CONTAINER"]
    AZURE_SEARCH_SERVICE = os.environ["AZURE_SEARCH_SERVICE"]
    AZURE_SEARCH_INDEX = os.environ["AZURE_SEARCH_INDEX"]
    # Shared by all OpenAI deployments
    OPENAI_HOST = os.getenv("OPENAI_HOST", "azure")
    OPENAI_CHATGPT_MODEL = os.environ["AZURE_OPENAI_CHATGPT_MODEL"]
    OPENAI_EMB_MODEL = os.getenv(
        "AZURE_OPENAI_EMB_MODEL_NAME", "text-embedding-3-large")
    # Used with Azure OpenAI deployments
    AZURE_OPENAI_SERVICE = os.getenv("AZURE_OPENAI_SERVICE")
    AZURE_OPENAI_CHATGPT_DEPLOYMENT = os.getenv(
        "AZURE_OPENAI_CHATGPT_DEPLOYMENT") if OPENAI_HOST == "azure" else None
    AZURE_OPENAI_EMB_DEPLOYMENT = os.getenv(
        "AZURE_OPENAI_EMB_DEPLOYMENT") if OPENAI_HOST == "azure" else None
    # Used only with non-Azure OpenAI deployments
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
    OPENAI_ORGANIZATION = os.getenv("OPENAI_ORGANIZATION")
    AZURE_USE_AUTHENTICATION = os.getenv(
        "AZURE_USE_AUTHENTICATION", "").lower() == "true"
    AZURE_SERVER_APP_ID = os.getenv("AZURE_SERVER_APP_ID")
    AZURE_SERVER_APP_SECRET = os.getenv("AZURE_SERVER_APP_SECRET")
    AZURE_CLIENT_APP_ID = os.getenv("AZURE_CLIENT_APP_ID")
    AZURE_TENANT_ID = os.getenv("AZURE_TENANT_ID")
    TOKEN_CACHE_PATH = os.getenv("TOKEN_CACHE_PATH")

    KB_FIELDS_CONTENT = os.getenv("KB_FIELDS_CONTENT", "content")
    KB_FIELDS_SOURCEPAGE = os.getenv("KB_FIELDS_SOURCEPAGE", "sourcepage")

    AZURE_SEARCH_QUERY_LANGUAGE = os.getenv(
        "AZURE_SEARCH_QUERY_LANGUAGE", "en-us")
    AZURE_SEARCH_QUERY_SPELLER = os.getenv(
        "AZURE_SEARCH_QUERY_SPELLER", "lexicon")
    # Use the current user identity to authenticate with Azure OpenAI, AI Search and Blob Storage (no secrets needed,
    # just use 'az login' locally, and managed identity when deployed on Azure). If you need to use keys, use separate AzureKeyCredential instances with the
    # keys for each service
    # If you encounter a blocking error during a DefaultAzureCredential resolution, you can exclude the problematic credential by using a parameter (ex. exclude_shared_token_cache_credential=True)
    azure_credential = DefaultAzureCredential(
        exclude_shared_token_cache_credential=True)

    # Set up authentication helper
    auth_helper = AuthenticationHelper(
        use_authentication=AZURE_USE_AUTHENTICATION,
        server_app_id=AZURE_SERVER_APP_ID,
        server_app_secret=AZURE_SERVER_APP_SECRET,
        client_app_id=AZURE_CLIENT_APP_ID,
        tenant_id=AZURE_TENANT_ID,
        token_cache_path=TOKEN_CACHE_PATH,
    )

    # Set up clients for AI Search and Storage
    search_client = SearchClient(
        endpoint=f"https://{AZURE_SEARCH_SERVICE}.search.windows.net",
        index_name=AZURE_SEARCH_INDEX,
        credential=azure_credential,
    )
    blob_client = BlobServiceClient(
        account_url=f"https://{AZURE_STORAGE_ACCOUNT}.blob.core.windows.net", credential=azure_credential
    )
    blob_container_client = blob_client.get_container_client(
        AZURE_STORAGE_CONTAINER)

    # Used by the OpenAI SDK
    openai_client: AsyncOpenAI

    if OPENAI_HOST == "azure":
        token_provider = get_bearer_token_provider(
            azure_credential, "https://cognitiveservices.azure.com/.default")
        # Store on app.config for later use inside requests
        openai_client = AsyncAzureOpenAI(
            api_version="2023-07-01-preview",
            azure_endpoint=f"https://{AZURE_OPENAI_SERVICE}.openai.azure.com",
            azure_ad_token_provider=token_provider,
        )
    else:
        openai_client = AsyncOpenAI(
            api_key=OPENAI_API_KEY,
            organization=OPENAI_ORGANIZATION,
        )

    current_app.config[CONFIG_OPENAI_CLIENT] = openai_client
    current_app.config[CONFIG_SEARCH_CLIENT] = search_client
    current_app.config[CONFIG_BLOB_CONTAINER_CLIENT] = blob_container_client
    current_app.config[CONFIG_AUTH_CLIENT] = auth_helper

    # Various approaches to integrate GPT and external knowledge, most applications will use a single one of these patterns
    # or some derivative, here we include several for exploration purposes
    current_app.config[CONFIG_ASK_APPROACH] = RetrieveThenReadApproach(
        search_client=search_client,
        openai_client=openai_client,
        chatgpt_model=OPENAI_CHATGPT_MODEL,
        chatgpt_deployment=AZURE_OPENAI_CHATGPT_DEPLOYMENT,
        embedding_model=OPENAI_EMB_MODEL,
        embedding_deployment=AZURE_OPENAI_EMB_DEPLOYMENT,
        sourcepage_field=KB_FIELDS_SOURCEPAGE,
        content_field=KB_FIELDS_CONTENT,
        query_language=AZURE_SEARCH_QUERY_LANGUAGE,
        query_speller=AZURE_SEARCH_QUERY_SPELLER,
    )

    current_app.config[CONFIG_CHAT_APPROACH] = ChatReadRetrieveReadApproach(
        search_client=search_client,
        openai_client=openai_client,
        chatgpt_model=OPENAI_CHATGPT_MODEL,
        chatgpt_deployment=AZURE_OPENAI_CHATGPT_DEPLOYMENT,
        embedding_model=OPENAI_EMB_MODEL,
        embedding_deployment=AZURE_OPENAI_EMB_DEPLOYMENT,
        sourcepage_field=KB_FIELDS_SOURCEPAGE,
        content_field=KB_FIELDS_CONTENT,
        query_language=AZURE_SEARCH_QUERY_LANGUAGE,
        query_speller=AZURE_SEARCH_QUERY_SPELLER,
    )


async def set_index_and_container(index, container):
    print("Setting index and container")
    print(index)
    print(container)
    AZURE_STORAGE_ACCOUNT = os.environ["AZURE_STORAGE_ACCOUNT"]
    AZURE_SEARCH_SERVICE = os.environ["AZURE_SEARCH_SERVICE"]
    # Shared by all OpenAI deployments
    OPENAI_HOST = os.getenv("OPENAI_HOST", "azure")
    OPENAI_CHATGPT_MODEL = os.environ["AZURE_OPENAI_CHATGPT_MODEL"]
    OPENAI_EMB_MODEL = os.getenv(
        "AZURE_OPENAI_EMB_MODEL_NAME", "text-embedding-3-large")
    # Used with Azure OpenAI deployments
    AZURE_OPENAI_CHATGPT_DEPLOYMENT = os.getenv(
        "AZURE_OPENAI_CHATGPT_DEPLOYMENT") if OPENAI_HOST == "azure" else None
    AZURE_OPENAI_EMB_DEPLOYMENT = os.getenv(
        "AZURE_OPENAI_EMB_DEPLOYMENT") if OPENAI_HOST == "azure" else None
    # Used only with non-Azure OpenAI deployments
    KB_FIELDS_CONTENT = os.getenv("KB_FIELDS_CONTENT", "content")
    KB_FIELDS_SOURCEPAGE = os.getenv("KB_FIELDS_SOURCEPAGE", "sourcepage")
    AZURE_SEARCH_QUERY_LANGUAGE = os.getenv(
        "AZURE_SEARCH_QUERY_LANGUAGE", "en-us")
    AZURE_SEARCH_QUERY_SPELLER = os.getenv(
        "AZURE_SEARCH_QUERY_SPELLER", "lexicon")

    azure_credential = DefaultAzureCredential(
        exclude_shared_token_cache_credential=True)

    search_client = SearchClient(
        endpoint=f"https://{AZURE_SEARCH_SERVICE}.search.windows.net",
        index_name=index,
        credential=azure_credential,
    )

    blob_client = BlobServiceClient(
        account_url=f"https://{AZURE_STORAGE_ACCOUNT}.blob.core.windows.net", credential=azure_credential
    )
    blob_container_client = blob_client.get_container_client(
        container)

    current_app.config[CONFIG_SEARCH_CLIENT] = search_client
    current_app.config[CONFIG_BLOB_CONTAINER_CLIENT] = blob_container_client

    current_app.config[CONFIG_ASK_APPROACH] = RetrieveThenReadApproach(
        search_client=search_client,
        openai_client=current_app.config[CONFIG_OPENAI_CLIENT],
        chatgpt_model=OPENAI_CHATGPT_MODEL,
        chatgpt_deployment=AZURE_OPENAI_CHATGPT_DEPLOYMENT,
        embedding_model=OPENAI_EMB_MODEL,
        embedding_deployment=AZURE_OPENAI_EMB_DEPLOYMENT,
        sourcepage_field=KB_FIELDS_SOURCEPAGE,
        content_field=KB_FIELDS_CONTENT,
        query_language=AZURE_SEARCH_QUERY_LANGUAGE,
        query_speller=AZURE_SEARCH_QUERY_SPELLER,
    )

    current_app.config[CONFIG_CHAT_APPROACH] = ChatReadRetrieveReadApproach(
        search_client=search_client,
        openai_client=current_app.config[CONFIG_OPENAI_CLIENT],
        chatgpt_model=OPENAI_CHATGPT_MODEL,
        chatgpt_deployment=AZURE_OPENAI_CHATGPT_DEPLOYMENT,
        embedding_model=OPENAI_EMB_MODEL,
        embedding_deployment=AZURE_OPENAI_EMB_DEPLOYMENT,
        sourcepage_field=KB_FIELDS_SOURCEPAGE,
        content_field=KB_FIELDS_CONTENT,
        query_language=AZURE_SEARCH_QUERY_LANGUAGE,
        query_speller=AZURE_SEARCH_QUERY_SPELLER,
    )

    return "Success"


def create_app():

    logging.info("CREATE APP 1000")
    logging.info(f"CREATE APP 1500")

    print("CREATE APP 200")
    logging.error(f"ERRoR TEST 40000")

    app = Quart(__name__)
    app.register_blueprint(bp)

    if os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING"):
        configure_azure_monitor()
        # This tracks HTTP requests made by aiohttp:
        AioHttpClientInstrumentor().instrument()
        # This tracks HTTP requests made by httpx/openai:
        HTTPXClientInstrumentor().instrument()
        # This middleware tracks app route requests:
        app.asgi_app = OpenTelemetryMiddleware(
            app.asgi_app)  # type: ignore[method-assign]

    # Level should be one of https://docs.python.org/3/library/logging.html#logging-levels
    default_level = "INFO"  # In development, log more verbosely
    if os.getenv("WEBSITE_HOSTNAME"):  # In production, don't log as heavily
        default_level = "WARNING"
    logging.basicConfig(level=os.getenv("APP_LOG_LEVEL", default_level))

    logger = logging.getLogger(__name__)
    handler = logging.StreamHandler(stream=sys.stdout)
    logger.addHandler(handler)

    logging.info("CREATE APP 1000 2")
    logging.error("ERRoR TEST 40000 3")

    app.logger.info("App logger")

    if allowed_origin := os.getenv("ALLOWED_ORIGIN"):
        app.logger.info("CORS enabled for %s", allowed_origin)
        cors(app, allow_origin=allowed_origin, allow_methods=["GET", "POST"])
    return app
