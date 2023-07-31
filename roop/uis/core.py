import importlib
from typing import Dict, Optional, List
import gradio

import roop.globals
from roop.uis.typing import Component, ComponentName
from roop.utilities import list_module_names

COMPONENTS: Dict[ComponentName, Component] = {}


def init() -> None:
    with gradio.Blocks(theme=get_theme()) as ui:
        ui_layout_module = importlib.import_module(f'roop.uis.__layouts__.{roop.globals.ui_layouts[0]}')
        ui_layout_module.render()
    ui.launch()


def get_theme() -> gradio.Theme:
    return gradio.themes.Soft(
        primary_hue=gradio.themes.colors.red,
        secondary_hue=gradio.themes.colors.gray,
        font=gradio.themes.GoogleFont('Open Sans')
    )


def get_component(name: ComponentName) -> Component:
    return COMPONENTS[name]


def register_component(name: ComponentName, component: Component) -> None:
    COMPONENTS[name] = component


def list_ui_layouts_names() -> Optional[List[str]]:
    return list_module_names('roop/uis/__layouts__')
