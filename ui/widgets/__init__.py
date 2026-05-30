"""UI 公共小部件。"""

from .auto_stack import AutoSizeStack
from .model_install_guide import (
    ModelInstallGuideDialog,
    build_model_install_markdown,
    find_matching_record_for_folder,
    install_model_folder,
    missing_python_deps,
    required_model_paths,
    show_model_install_guide,
    start_dependency_install_if_needed,
)
from .window_chrome import (
    DragBar,
    FramelessDialog,
    apply_rounded_mask,
    attach_size_grip,
    fade_in_window,
    install_window_drag,
    install_window_resize,
    make_window_controls,
    native_resize_hit_test,
    position_size_grip,
    show_message,
)

__all__ = [
    "AutoSizeStack",
    "DragBar",
    "FramelessDialog",
    "ModelInstallGuideDialog",
    "apply_rounded_mask",
    "attach_size_grip",
    "build_model_install_markdown",
    "fade_in_window",
    "find_matching_record_for_folder",
    "install_model_folder",
    "install_window_drag",
    "install_window_resize",
    "make_window_controls",
    "missing_python_deps",
    "native_resize_hit_test",
    "position_size_grip",
    "required_model_paths",
    "show_message",
    "show_model_install_guide",
    "start_dependency_install_if_needed",
]
