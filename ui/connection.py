"""Connection operators used by the Clips sidebar."""

import webbrowser

import bpy
from bpy.types import Operator

from ..common.config import Config
from ..core.storage import Storage
from ..server.auth_server import PluginAuthServer
from ..server import models
from . import track_generation


def _tag_view3d_redraw(context) -> None:
    screen = getattr(context, "screen", None)
    if screen is None:
        return
    for area in screen.areas:
        if area.type == 'VIEW_3D':
            area.tag_redraw()


class AVACAPO_OT_login_browser(Operator):
    """Open browser to create an API token and receive it automatically."""

    bl_idname = "avacapo.login_browser"
    bl_label = "Login via Browser"

    _auth_server = None
    _timer = None

    def _finish(self, context, *, message, level):
        if self._timer is not None:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None
        self._auth_server = None
        _tag_view3d_redraw(context)
        self.report({level}, message)

    def modal(self, context, event):
        if event.type == 'ESC':
            if self._auth_server is not None:
                self._auth_server.stop()
            self._finish(context, message="Browser login cancelled", level='INFO')
            return {'CANCELLED'}
        if event.type != 'TIMER':
            return {'PASS_THROUGH'}
        if self._auth_server is None or self._auth_server.is_running:
            return {'RUNNING_MODAL'}

        token = self._auth_server.token
        if token:
            Storage.api_token = token
            try:
                Storage.save()
            except OSError:
                self._finish(context, message="Could not save the API token", level='ERROR')
                return {'CANCELLED'}
            models.reset_model_enum_items()
            track_generation.refresh_model_catalog()
            self._finish(context, message="Connected successfully!", level='INFO')
            return {'FINISHED'}

        self._finish(context, message="Auth timed out or was cancelled", level='WARNING')
        return {'CANCELLED'}

    def invoke(self, context, _event):
        self._auth_server = PluginAuthServer()
        try:
            port = self._auth_server.start()
        except OSError:
            self.report({'ERROR'}, "Could not start the local login server")
            return {'CANCELLED'}
        url = f"{Config.FRONTEND_URL.rstrip('/')}/app/api-tokens?plugin_auth=true&port={port}"
        webbrowser.open(url)
        self._timer = context.window_manager.event_timer_add(0.5, window=context.window)
        context.window_manager.modal_handler_add(self)
        self.report({'INFO'}, "Waiting for browser auth...")
        return {'RUNNING_MODAL'}


class AVACAPO_OT_paste_token(Operator):
    """Save a manually pasted API token."""

    bl_idname = "avacapo.paste_token"
    bl_label = "Connect"

    def execute(self, context):
        data = context.scene.clip_data
        token = data.token_input.strip()
        if not token:
            self.report({'WARNING'}, "Token is empty")
            return {'CANCELLED'}
        Storage.api_token = token
        try:
            Storage.save()
        except OSError:
            self.report({'ERROR'}, "Could not save the API token")
            return {'CANCELLED'}
        data.token_input = ""
        models.reset_model_enum_items()
        track_generation.refresh_model_catalog()
        _tag_view3d_redraw(context)
        self.report({'INFO'}, "Connected successfully!")
        return {'FINISHED'}


class AVACAPO_OT_disconnect(Operator):
    """Clear the stored API token."""

    bl_idname = "avacapo.disconnect"
    bl_label = "Disconnect"

    def execute(self, context):
        Storage.api_token = ""
        try:
            Storage.save()
        except OSError:
            self.report({'ERROR'}, "Could not clear the API token")
            return {'CANCELLED'}
        models.reset_model_enum_items()
        _tag_view3d_redraw(context)
        self.report({'INFO'}, "Disconnected")
        return {'FINISHED'}
