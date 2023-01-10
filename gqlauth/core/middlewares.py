import asyncio
from typing import TYPE_CHECKING, Callable, Optional, Union

from asgiref.sync import sync_to_async
from django.contrib.auth import login
from django.contrib.auth.models import AnonymousUser
from django.core.handlers.asgi import ASGIRequest
from django.http import HttpRequest
from strawberry import Schema

from gqlauth.core.exceptions import TokenExpired
from gqlauth.core.types_ import GQLAuthError, GQLAuthErrors
from gqlauth.core.utils import USER_UNION, app_settings
from gqlauth.jwt.types_ import TokenType
from jwt import PyJWTError

anon_user = AnonymousUser()
if TYPE_CHECKING:
    pass


class UserOrError:
    __slots__ = ("user", "error")

    def __init__(self, user: USER_UNION = anon_user, error: Exception = None):
        self.user = user
        self.error = error

    def authorized_user(self) -> Optional[USER_UNION]:
        if self.user.is_authenticated:  # real django user model always returns true.
            return self.user
        return None


USER_OR_ERROR_KEY = UserOrError.__name__


def get_user_or_error(scope_or_request: Union[dict, HttpRequest]) -> UserOrError:
    user_or_error = UserOrError()
    if token := app_settings.JWT_TOKEN_FINDER(scope_or_request):
        try:
            token = TokenType.from_token(token=token)
            user = token.get_user_instance()
            user_or_error.user = user

        except PyJWTError:  # raised by python-jwt
            user_or_error.error = GQLAuthError(code=GQLAuthErrors.INVALID_TOKEN)

        except TokenExpired:
            user_or_error.error = GQLAuthError(code=GQLAuthErrors.EXPIRED_TOKEN)
        except Exception as exc:  # pragma: no cover
            import traceback

            traceback.print_tb(exc.__traceback__)
            print(exc)
            raise exc
    else:
        user_or_error.error = GQLAuthError(code=GQLAuthErrors.MISSING_TOKEN)

    return user_or_error


get_user_or_error_async = sync_to_async(get_user_or_error)


def channels_jwt_middleware(inner: Callable):
    from channels.auth import (
        login as channels_login,  # deferred import for users that don't use channels.
    )

    if asyncio.iscoroutinefunction(inner):

        async def middleware(scope, receive, send):
            if not scope.get(USER_OR_ERROR_KEY, None):
                user_or_error: UserOrError = await get_user_or_error_async(scope)
                scope[USER_OR_ERROR_KEY] = user_or_error
                if user := user_or_error.authorized_user():
                    await channels_login(scope, user)
            return await inner(scope, receive, send)

    else:  # pragma: no cover.
        raise NotImplementedError("sync channels middleware is not supported yet.")
    return middleware


def django_jwt_middleware(get_response):
    if asyncio.iscoroutinefunction(get_response):
        login_async = sync_to_async(login)

        async def middleware(request: ASGIRequest):
            if not hasattr(request, USER_OR_ERROR_KEY):
                user_or_error: UserOrError = await get_user_or_error_async(request)
                setattr(request, USER_OR_ERROR_KEY, user_or_error)
                if user := user_or_error.authorized_user():
                    await login_async(request, user)
            return await get_response(request)

    else:

        def middleware(request: HttpRequest):
            if not hasattr(request, USER_OR_ERROR_KEY):
                user_or_error: UserOrError = get_user_or_error(request)
                setattr(request, USER_OR_ERROR_KEY, user_or_error)
                if user := user_or_error.authorized_user():
                    login(request, user)
            return get_response(request)

    return middleware


class JwtSchema(Schema):
    """injects token to context."""

    def execute_sync(self, *args, **kwargs):
        self._inject_user_and_errors(kwargs)
        return super().execute_sync(*args, **kwargs)

    async def execute(self, *args, **kwargs):
        self._inject_user_and_errors(kwargs)
        return await super().execute(*args, **kwargs)

    def subscribe(self, *args, **kwargs):
        self._inject_user_and_errors(kwargs)
        res = super().subscribe(*args, **kwargs)
        return res

    @staticmethod
    def _inject_user_and_errors(kwargs: dict) -> UserOrError:
        context = kwargs.get("context_value")
        # channels compat
        if ws := getattr(context, "ws", None):
            user_or_error: UserOrError = ws.scope[USER_OR_ERROR_KEY]
        else:
            user_or_error: UserOrError = getattr(context.request, USER_OR_ERROR_KEY)  # type: ignore
        context.request.user = user_or_error.user  # type: ignore
        return user_or_error
