from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route


async def stats(_request: Request) -> JSONResponse:
    return JSONResponse({"status": "booting"})


app = Starlette(routes=[Route("/stats", stats, methods=["GET"])])


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=4000)
