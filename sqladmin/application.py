import inspect
from typing import Any, Callable, List, Optional, Sequence, Type, Union, no_type_check

from jinja2 import ChoiceLoader, FileSystemLoader, PackageLoader
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.orm import Session, sessionmaker
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from sqladmin._types import ENGINE_TYPE
from sqladmin.models import BaseView, ModelView

__all__ = [
    "Admin",
    "expose",
    "action",
]


class BaseAdmin:
    """Base class for implementing Admin interface.

    Danger:
        This class should almost never be used directly.
    """

    def __init__(
        self,
        app: Starlette,
        engine: ENGINE_TYPE,
        base_url: str = "/admin",
        title: str = "Admin",
        logo_url: str = None,
        templates_dir: str = "templates",
        middlewares: Optional[Sequence[Middleware]] = None,
    ) -> None:
        self.app = app
        self.engine = engine
        self.base_url = base_url
        self.templates_dir = templates_dir
        self.admin = Starlette(middleware=middlewares)
        self._views: List[Union[BaseView, ModelView]] = []

        self.templates = self.init_templating_engine(title=title, logo_url=logo_url)

    def init_templating_engine(
        self, title: str, logo_url: str = None
    ) -> Jinja2Templates:
        templates = Jinja2Templates("templates")
        loaders = [
            FileSystemLoader(self.templates_dir),
            PackageLoader("sqladmin", "templates"),
        ]

        templates.env.loader = ChoiceLoader(loaders)
        templates.env.globals["min"] = min
        templates.env.globals["zip"] = zip
        templates.env.globals["admin_title"] = title
        templates.env.globals["admin_logo_url"] = logo_url
        templates.env.globals["views"] = self.views
        templates.env.globals["is_list"] = lambda x: isinstance(x, list)

        return templates

    @property
    def views(self) -> List[Union[BaseView, ModelView]]:
        """Get list of ModelView and BaseView instances lazily.

        Returns:
            List of ModelView and BaseView instances added to Admin.
        """

        return self._views

    def _find_model_view(self, identity: str) -> ModelView:
        for view in self.views:
            if isinstance(view, ModelView) and view.identity == identity:
                return view

        raise HTTPException(status_code=404)

    def add_view(self, view: Union[Type[ModelView], Type[BaseView]]) -> None:
        """Add ModelView or BaseView classes to Admin.
        This is a shortcut that will handle both `add_model_view` and `add_base_view`.
        """

        view.url_path_for = self.app.url_path_for

        if view.is_model:
            self.add_model_view(view)  # type: ignore
        else:
            self.add_base_view(view)

    def add_model_view(self, view: Type[ModelView]) -> None:
        """Add ModelView to the Admin.

        ???+ usage
            ```python
            from sqladmin import Admin, ModelView

            class UserAdmin(ModelView, model=User):
                pass

            admin.add_model_view(UserAdmin)
            ```
        """

        # Set database engine from Admin instance
        view.engine = self.engine

        if isinstance(view.engine, Engine):
            view.sessionmaker = sessionmaker(
                bind=view.engine,
                class_=Session,
                autoflush=False,
                autocommit=False,
            )
            view.async_engine = False
        else:
            view.sessionmaker = sessionmaker(
                bind=view.engine,
                class_=AsyncSession,
                autoflush=False,
                autocommit=False,
            )
            view.async_engine = True

        view_instance = view()
        funcs = inspect.getmembers(view_instance, predicate=inspect.ismethod)

        for _, func in funcs[::-1]:
            if hasattr(func, "_action"):
                self.admin.add_route(
                    route=func,
                    path="/{identity}/action/{pk}/" + func._name,
                    methods=["GET", "POST"],
                    name=f"{view_instance.identity}-{func._name}",
                    include_in_schema=func._include_in_schema,
                )

                if func._add_in_list:
                    view.custom_actions_in_list[func._name] = func._label
                if func._add_in_detail:
                    view.custom_actions_in_detail[func._name] = func._label

                if func._confirmation_message:
                    view.custom_actions_confirmation[
                        func._name
                    ] = func._confirmation_message

        view.templates = self.templates
        self._views.append((view_instance))

    def add_base_view(self, view: Type[BaseView]) -> None:
        """Add BaseView to the Admin.

        ???+ usage
            ```python
            from sqladmin import BaseView, expose

            class CustomAdmin(BaseView):
                name = "Custom Page"
                icon = "fa-solid fa-chart-line"

                @expose("/custom", methods=["GET"])
                def test_page(self, request: Request):
                    return self.templates.TemplateResponse(
                        "custom.html",
                        context={"request": request},
                    )

            admin.add_base_view(CustomAdmin)
            ```
        """

        view_instance = view()
        funcs = inspect.getmembers(view_instance, predicate=inspect.ismethod)

        for _, func in funcs[::-1]:
            if hasattr(func, "_exposed"):
                self.admin.add_route(
                    route=func,
                    path=func._path,
                    methods=func._methods,
                    name=func._identity,
                    include_in_schema=func._include_in_schema,
                )

                view.identity = func._identity

        view.templates = self.templates
        self._views.append(view_instance)

    def register_model(self, model: Type[ModelView]) -> None:  # pragma: no cover
        import warnings

        warnings.warn(
            "Method `register_model` is deprecated please use `add_view` instead.",
            DeprecationWarning,
        )
        self.add_view(model)


class BaseAdminView(BaseAdmin):
    """
    Manage right to access to an action from a model
    """

    async def _list(self, request: Request) -> None:
        model_view = self._find_model_view(request.path_params["identity"])
        if not model_view.is_accessible(request):
            raise HTTPException(status_code=403)

    async def _create(self, request: Request) -> None:
        model_view = self._find_model_view(request.path_params["identity"])
        if not model_view.can_create or not model_view.is_accessible(request):
            raise HTTPException(status_code=403)

    async def _details(self, request: Request) -> None:
        model_view = self._find_model_view(request.path_params["identity"])
        if not model_view.can_view_details or not model_view.is_accessible(request):
            raise HTTPException(status_code=403)

    async def _delete(self, request: Request) -> None:
        model_view = self._find_model_view(request.path_params["identity"])
        if not model_view.can_delete or not model_view.is_accessible(request):
            raise HTTPException(status_code=403)

    async def _edit(self, request: Request) -> None:
        model_view = self._find_model_view(request.path_params["identity"])
        if not model_view.can_edit or not model_view.is_accessible(request):
            raise HTTPException(status_code=403)

    async def _export(self, request: Request) -> None:
        model_view = self._find_model_view(request.path_params["identity"])
        if not model_view.can_export or not model_view.is_accessible(request):
            raise HTTPException(status_code=403)
        if request.path_params["export_type"] not in model_view.export_types:
            raise HTTPException(status_code=404)


class Admin(BaseAdminView):
    """Main entrypoint to admin interface.

    ???+ usage
        ```python
        from fastapi import FastAPI
        from sqladmin import Admin, ModelView

        from mymodels import User # SQLAlchemy model


        app = FastAPI()
        admin = Admin(app, engine)


        class UserAdmin(ModelView, model=User):
            column_list = [User.id, User.name]


        admin.add_view(UserAdmin)
        ```
    """

    def __init__(
        self,
        app: Starlette,
        engine: ENGINE_TYPE,
        base_url: str = "/admin",
        title: str = "Admin",
        logo_url: str = None,
        middlewares: Optional[Sequence[Middleware]] = None,
        debug: bool = False,
        templates_dir: str = "templates",
    ) -> None:
        """
        Args:
            app: Starlette or FastAPI application.
            engine: SQLAlchemy engine instance.
            base_url: Base URL for Admin interface.
            title: Admin title.
            logo_url: URL of logo to be displayed instead of title.
        """

        assert isinstance(engine, (Engine, AsyncEngine))
        super().__init__(
            app=app,
            engine=engine,
            base_url=base_url,
            title=title,
            logo_url=logo_url,
            templates_dir=templates_dir,
            middlewares=middlewares,
        )

        statics = StaticFiles(packages=["sqladmin"])

        def http_exception(request: Request, exc: Exception) -> Response:
            assert isinstance(exc, HTTPException)
            context = {
                "request": request,
                "status_code": exc.status_code,
                "message": exc.detail,
            }
            return self.templates.TemplateResponse(
                "error.html", context, status_code=exc.status_code
            )

        routes = [
            Mount("/statics", app=statics, name="statics"),
            Route("/", endpoint=self.index, name="index"),
            Route("/{identity}/list", endpoint=self.list, name="list"),
            Route("/{identity}/details/{pk}", endpoint=self.details, name="details"),
            Route(
                "/{identity}/delete/{pk}",
                endpoint=self.delete,
                name="delete",
                methods=["DELETE"],
            ),
            Route(
                "/{identity}/create",
                endpoint=self.create,
                name="create",
                methods=["GET", "POST"],
            ),
            Route(
                "/{identity}/edit/{pk}",
                endpoint=self.edit,
                name="edit",
                methods=["GET", "POST"],
            ),
            Route(
                "/{identity}/export/{export_type}",
                endpoint=self.export,
                name="export",
                methods=["GET"],
            ),
        ]

        self.admin.router.routes = routes
        self.admin.exception_handlers = {HTTPException: http_exception}
        self.admin.debug = debug
        self.app.mount(base_url, app=self.admin, name="admin")

    async def index(self, request: Request) -> Response:
        """Index route which can be overridden to create dashboards."""

        return self.templates.TemplateResponse("index.html", {"request": request})

    async def list(self, request: Request) -> Response:
        """List route to display paginated Model instances."""

        await self._list(request)

        model_view = self._find_model_view(request.path_params["identity"])

        page = int(request.query_params.get("page", 1))
        page_size = int(request.query_params.get("pageSize", 0))
        search = request.query_params.get("search", None)
        sort_by = request.query_params.get("sortBy", None)
        sort = request.query_params.get("sort", "asc")

        pagination = await model_view.list(page, page_size, search, sort_by, sort)
        pagination.add_pagination_urls(request.url)

        context = {
            "request": request,
            "model_view": model_view,
            "pagination": pagination,
        }

        return self.templates.TemplateResponse(model_view.list_template, context)

    async def details(self, request: Request) -> Response:
        """Details route."""

        await self._details(request)

        model_view = self._find_model_view(request.path_params["identity"])

        model = await model_view.get_model_by_pk(request.path_params["pk"])
        if not model:
            raise HTTPException(status_code=404)

        context = {
            "request": request,
            "model_view": model_view,
            "model": model,
            "title": model_view.name,
        }

        return self.templates.TemplateResponse(model_view.details_template, context)

    async def delete(self, request: Request) -> Response:
        """Delete route."""

        await self._delete(request)

        identity = request.path_params["identity"]
        model_view = self._find_model_view(identity)

        model = await model_view.get_model_by_pk(request.path_params["pk"])
        if not model:
            raise HTTPException(status_code=404)

        await model_view.delete_model(model)

        return Response(content=request.url_for("admin:list", identity=identity))

    async def create(self, request: Request) -> Response:
        """Create model endpoint."""

        await self._create(request)

        identity = request.path_params["identity"]
        model_view = self._find_model_view(identity)

        Form = await model_view.scaffold_form()
        form = Form(await request.form())

        context = {
            "request": request,
            "model_view": model_view,
            "form": form,
        }

        if request.method == "GET":
            return self.templates.TemplateResponse(model_view.create_template, context)

        if not form.validate():
            return self.templates.TemplateResponse(
                model_view.create_template,
                context,
                status_code=400,
            )

        await model_view.insert_model(form.data)

        return RedirectResponse(
            request.url_for("admin:list", identity=identity),
            status_code=302,
        )

    async def edit(self, request: Request) -> Response:
        """Edit model endpoint."""

        await self._edit(request)

        identity = request.path_params["identity"]
        model_view = self._find_model_view(identity)

        model = await model_view.get_model_by_pk(request.path_params["pk"])
        if not model:
            raise HTTPException(status_code=404)

        Form = await model_view.scaffold_form()
        context = {
            "request": request,
            "model_view": model_view,
        }

        if request.method == "GET":
            context["form"] = Form(obj=model)
            return self.templates.TemplateResponse(model_view.edit_template, context)

        form = Form(await request.form())
        if not form.validate():
            context["form"] = form
            return self.templates.TemplateResponse(
                model_view.edit_template,
                context,
                status_code=400,
            )

        await model_view.update_model(pk=request.path_params["pk"], data=form.data)

        return RedirectResponse(
            request.url_for("admin:list", identity=identity),
            status_code=302,
        )

    async def export(self, request: Request) -> Response:
        """Export model endpoint."""

        await self._export(request)

        identity = request.path_params["identity"]
        export_type = request.path_params["export_type"]

        model_view = self._find_model_view(identity)
        rows = await model_view.get_model_objects(limit=model_view.export_max_rows)
        return model_view.export_data(rows, export_type=export_type)


def expose(
    path: str,
    *,
    methods: List[str] = ["GET"],
    identity: str = None,
    include_in_schema: bool = True,
) -> Callable[..., Any]:
    """Expose View with information."""

    @no_type_check
    def wrap(func):
        func._exposed = True
        func._path = path
        func._methods = methods
        func._identity = identity or func.__name__
        func._include_in_schema = include_in_schema
        return func

    return wrap


def action(
    name: str,
    label: str = None,
    confirmation_message: str = None,
    *,
    include_in_schema: bool = True,
    add_in_detail: bool = True,
    add_in_list: bool = True,
) -> Callable[..., Any]:
    """Expose View with information."""

    @no_type_check
    def wrap(func):
        func._action = True
        func._name = name
        func._label = label or name
        func._confirmation_message = confirmation_message
        func._include_in_schema = include_in_schema
        func._add_in_detail = add_in_detail
        func._add_in_list = add_in_list
        return func

    return wrap
