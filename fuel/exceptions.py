from rest_framework.views import exception_handler


def api_exception_handler(exc, context):
    """Render every DRF error (bad JSON, 405, ...) in the API's {"error": {"message": ...}} shape."""
    response = exception_handler(exc, context)
    if response is not None and isinstance(response.data, dict) and "detail" in response.data:
        response.data = {"error": {"message": str(response.data["detail"])}}
    return response
