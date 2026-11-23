"""Publish the agreed v1 wire contracts for operations registered in this app."""

from copy import deepcopy
from functools import cache

from fastapi import FastAPI
from fastapi.responses import FileResponse
from yaml import safe_load

from dovideo.application.spec_contracts import contracts_dir


@cache
def _target_contract():
    return safe_load((contracts_dir() / "openapi.yaml").read_text(encoding="utf-8"))


def install_spec_openapi(app: FastAPI) -> None:
    @app.get("/schemas/domain.schema.json", include_in_schema=False)
    def domain_schema():
        return FileResponse(contracts_dir() / "schemas/domain.schema.json",
                            media_type="application/schema+json")

    generated_openapi = app.openapi

    def openapi():
        if app.openapi_schema is None:
            published = generated_openapi()
            contract = _target_contract()
            published["components"].setdefault("securitySchemes", {}).update(
                deepcopy(contract["components"]["securitySchemes"])
            )
            for path, methods in published["paths"].items():
                target = contract["paths"].get(path, {})
                for method in methods.keys() & target.keys():
                    parameters = methods[method].get("parameters", [])
                    methods[method] = deepcopy(target[method])
                    if (path, method) == ("/v1/render-jobs", "post"):
                        methods[method]["parameters"] = [p for p in parameters if p["name"] == "format"]
                    methods[method].setdefault("security", deepcopy(contract["security"]))
        return app.openapi_schema

    app.openapi = openapi
