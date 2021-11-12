import inspect
import warnings
from collections import defaultdict, namedtuple
from typing import Any, Callable, Dict, Generator, List, Tuple

import pydantic
from django.http import HttpRequest

from ninja import UploadedFile, params
from ninja.compatibility.util import get_origin as get_collection_origin
from ninja.errors import ConfigError
from ninja.params import Body, File, Form, _MultiPartBody
from ninja.params_models import TModel, TModels
from ninja.signature.utils import get_path_param_names, get_typed_signature

__all__ = [
    "ViewSignature",
    "is_pydantic_model",
    "is_collection_type",
    "detect_collection_fields",
]

FuncParam = namedtuple(
    "FuncParam", ["name", "alias", "source", "annotation", "is_collection"]
)


class ViewSignature:
    FLATTEN_PATH_SEP = (
        "\x1e"  # ASCII Record Separator.  IE: not generally used in query names
    )

    def __init__(self, path: str, view_func: Callable) -> None:
        self.view_func = view_func
        self.signature = get_typed_signature(self.view_func)
        self.path = path
        self.path_params_names = get_path_param_names(path)
        self.docstring = inspect.cleandoc(view_func.__doc__ or "")
        self.has_kwargs = False

        self.params = []
        for pos, (name, arg) in enumerate(self.signature.parameters.items()):

            if arg.kind == arg.VAR_KEYWORD:
                # Skipping **kwargs
                self.has_kwargs = True
                continue

            if arg.kind == arg.VAR_POSITIONAL:
                # Skipping *args
                continue

            func_param = self._get_param_type(pos, name, arg)
            self.params.append(func_param)

        if hasattr(view_func, "_ninja_contribute_args"):
            # _ninja_contribute_args is a special attribute
            # which allows developers to create custom function params
            # inside decorators or other functions
            for p_name, p_type, p_source in view_func._ninja_contribute_args:  # type: ignore
                self.params.append(
                    FuncParam(p_name, p_source.alias or p_name, p_source, p_type, False)
                )

        self.models: TModels = self._create_models()

        self._validate_view_path_params()

    def _validate_view_path_params(self) -> None:
        """verify all path params are present in the path model fields"""
        if self.path_params_names:
            path_model = next(
                (m for m in self.models if m._param_source == "path"), None
            )
            missing = tuple(
                sorted(
                    name
                    for name in self.path_params_names
                    if not (path_model and name in path_model._flatten_map)
                )
            )
            if missing:
                warnings.warn_explicit(
                    UserWarning(
                        f"Field(s) {missing} are in the view path, but were not found in the view signature."
                    ),
                    category=None,
                    filename=inspect.getfile(self.view_func),
                    lineno=inspect.getsourcelines(self.view_func)[1],
                    source=None,
                )

    def _create_models(self) -> TModels:
        params_by_source_cls: Dict[Any, List[FuncParam]] = defaultdict(list)
        for param in self.params:
            param_source_cls = type(param.source)
            params_by_source_cls[param_source_cls].append(param)

        is_multipart_response_with_body = Body in params_by_source_cls and (
            File in params_by_source_cls or Form in params_by_source_cls
        )
        if is_multipart_response_with_body:
            params_by_source_cls[_MultiPartBody] = params_by_source_cls.pop(Body)

        result = []
        for param_cls, args in params_by_source_cls.items():
            cls_name: str = param_cls.__name__ + "Params"
            base_cls = param_cls._model
            attrs = {i.name: i.source for i in args}
            attrs["_param_source"] = param_cls._param_source()
            attrs["_flatten_map_reverse"] = {}

            if attrs["_param_source"] == "_request":
                attrs["_single_attr"] = args[0].name

            elif attrs["_param_source"] == "file":
                pass

            elif attrs["_param_source"] in {
                "form",
                "query",
                "header",
                "cookie",
                "path",
            }:
                flatten_map = self._args_flatten_map(args)
                attrs["_flatten_map"] = flatten_map
                attrs["_flatten_map_reverse"] = {
                    v: (k,) for k, v in flatten_map.items()
                }

            else:
                assert attrs["_param_source"] == "body"
                if is_multipart_response_with_body:
                    attrs["_body_params"] = {i.alias: i.annotation for i in args}
                else:
                    # ::TODO:: this is still sus.  build some test cases
                    attrs["_single_attr"] = args[0].name if len(args) == 1 else None

            # adding annotations
            attrs["__annotations__"] = {i.name: i.annotation for i in args}

            # collection fields:
            attrs["_collection_fields"] = detect_collection_fields(
                args, attrs.get("_flatten_map", {})
            )

            model_cls = type(cls_name, (base_cls,), attrs)
            # TODO: https://pydantic-docs.helpmanual.io/usage/models/#dynamic-model-creation - check if anything special in create_model method that I did not use
            result.append(model_cls)
        return result

    def _args_flatten_map(self, args: List[FuncParam]) -> Dict[str, Tuple[str, ...]]:
        flatten_map = {}
        arg_names: Any = {}
        for arg in args:
            if is_pydantic_model(arg.annotation):
                for name, path in self._model_flatten_map(arg.annotation, arg.alias):
                    if name in flatten_map:
                        raise ConfigError(
                            f"Duplicated name: '{name}' in params: '{arg_names[name]}' & '{arg.name}'"
                        )
                    flatten_map[name] = tuple(path.split(self.FLATTEN_PATH_SEP))
                    arg_names[name] = arg.name
            else:
                name = arg.alias
                if name in flatten_map:
                    raise ConfigError(
                        f"Duplicated name: '{name}' also in '{arg_names[name]}'"
                    )
                flatten_map[name] = (name,)
                arg_names[name] = name

        return flatten_map

    def _model_flatten_map(self, model: TModel, prefix: str) -> Generator:
        for field in model.__fields__.values():
            field_name = field.alias
            name = f"{prefix}{self.FLATTEN_PATH_SEP}{field_name}"
            if is_pydantic_model(field.type_):
                yield from self._model_flatten_map(field.type_, name)
            else:
                yield field_name, name

    def _get_param_type(self, pos: int, name: str, arg: inspect.Parameter) -> FuncParam:
        # _EMPTY = self.signature.empty
        annotation = arg.annotation

        print(" !!!! ", self.signature, name, pos, annotation)
        if self._is_http_request_arg(pos, name, arg):
            annotation = HttpRequest

        if annotation == self.signature.empty:
            if arg.default == self.signature.empty:
                annotation = str
            else:
                if isinstance(arg.default, params.Param):
                    annotation = type(arg.default.default)
                else:
                    annotation = type(arg.default)

        if annotation == type(None) or annotation == type(Ellipsis):  # noqa
            annotation = str

        is_collection = is_collection_type(annotation)

        if annotation == UploadedFile or (
            is_collection and annotation.__args__[0] == UploadedFile
        ):
            # People often forgot to mark UploadedFile as a File, so we better assign it automatically
            if arg.default == self.signature.empty or arg.default is None:
                default = arg.default == self.signature.empty and ... or arg.default
                return FuncParam(name, name, File(default), annotation, is_collection)

        param_source: params.Param

        # 0) if request
        if annotation == HttpRequest:
            param_source = params._Request(...)
            annotation = Any  # dropping http annotation as it will be just handeld by param model

        # 1) if type of the param is defined as one of the Param's subclasses - we just use that definition
        elif isinstance(arg.default, params.Param):
            param_source = arg.default

        # 2) if param name is a part of the path parameter
        elif name in self.path_params_names:
            assert (
                arg.default == self.signature.empty
            ), f"'{name}' is a path param, default not allowed"
            param_source = params.Path(...)

        # 3) if param is a collection, or annotation is part of pydantic model:
        elif is_collection or is_pydantic_model(annotation):
            if arg.default == self.signature.empty:
                param_source = params.Body(...)
            else:
                param_source = params.Body(arg.default)

        # 4) the last case is query param
        else:
            if arg.default == self.signature.empty:
                param_source = params.Query(...)
            else:
                param_source = params.Query(arg.default)

        return FuncParam(
            name, param_source.alias or name, param_source, annotation, is_collection
        )

    def _is_http_request_arg(self, pos: int, name: str, arg: inspect.Parameter) -> bool:
        # argument is request if it's annotated with HttpRequest
        # or it just blank "request" name without defaults and annotations
        if arg.annotation == HttpRequest:
            return True
        if (
            arg.annotation == self.signature.empty
            and name == "request"
            and pos == 0
            and arg.default == self.signature.empty
        ):
            return True
        return False


def is_pydantic_model(cls: Any) -> bool:
    try:
        return issubclass(cls, pydantic.BaseModel)
    except TypeError:
        return False


def is_collection_type(annotation: Any) -> bool:
    origin = get_collection_origin(annotation)
    types = (List, list, set, tuple)
    if origin is None:
        return issubclass(annotation, types)
    else:
        return origin in types  # TODO: I guess we should handle only list


def detect_collection_fields(
    args: List[FuncParam], flatten_map: Dict[str, Tuple[str, ...]]
) -> List[str]:
    """
    QueryDict has values that are always lists, so we need to help django ninja to understand
    better the input parameters if it's a list or a single value
    This method detects attributes that should be treated by ninja as lists and returns this list as a result
    """
    result = [i.name for i in args if i.is_collection]

    if flatten_map:
        args_d = {arg.alias: arg for arg in args}
        for path in (p for p in flatten_map.values() if len(p) > 1):
            annotation_or_field = args_d[path[0]].annotation
            for attr in path[1:]:
                annotation_or_field = next(
                    (
                        a
                        for a in annotation_or_field.__fields__.values()
                        if a.alias == attr
                    ),
                    annotation_or_field.__fields__.get(attr),
                )  # pragma: no cover

                annotation_or_field = getattr(
                    annotation_or_field, "outer_type_", annotation_or_field
                )

            if is_collection_type(annotation_or_field):
                result.append(path[-1])

    return result
