"""
OnDeskBot 坐标变换（纯数学模块，无硬件依赖）。

坐标约定 v6:
  - 传感器先校正实机水平镜像；校正后的 JPEG 左上角为原点，cam_x 向右，cam_y 向下。
  - 机械臂: Home 位于手机左上侧，+X 对应手机左→右，+Y 对应手机上→下。
  - 手机边界: X 轴满足 LEFT < RIGHT，Y 轴满足 TOP < BOTTOM。

因此，在屏幕 ROI 内归一化后:
  screen_x = cam_x
  screen_y = cam_y

本模块保持为纯 Python，既可在 MicroPython 上运行，也可在电脑端进行单元测试。
"""


COORDINATE_SYSTEM_VERSION = 6
EXPECTED_ARM_TRAVEL_X_MM = 99.55
EXPECTED_ARM_TRAVEL_Y_MM = 177.0


def clamp01(value):
    """将数值限制到 0.0~1.0。"""
    return max(0.0, min(1.0, float(value)))


def camera_pixel_to_screen_ratio(px, py, x1, y1, x2, y2):
    """
    将已去镜像且方向正常的 JPEG 像素坐标转换为 v6 屏幕比例坐标。

    ROI 使用 JPEG 的矩形边界 (x1, y1, x2, y2)。返回值:
      screen_x: 0=屏幕左边，1=屏幕右边（沿机械 +X）
      screen_y: 0=屏幕顶边，1=屏幕底边（沿机械 +Y）
    """
    roi_w = max(1.0, float(x2) - float(x1))
    roi_h = max(1.0, float(y2) - float(y1))
    cam_x = clamp01((float(px) - float(x1)) / roi_w)
    cam_y = clamp01((float(py) - float(y1)) / roi_h)
    return cam_x, cam_y


def screen_ratio_to_camera_pixel(screen_x, screen_y, x1, y1, x2, y2):
    """camera_pixel_to_screen_ratio() 的逆变换。"""
    screen_x = clamp01(screen_x)
    screen_y = clamp01(screen_y)
    cam_x = screen_x
    cam_y = screen_y
    px = float(x1) + cam_x * (float(x2) - float(x1))
    py = float(y1) + cam_y * (float(y2) - float(y1))
    return px, py


def screen_ratio_to_touch_mm(screen_x, screen_y,
                             d_left, d_right, d_top, d_bottom):
    """
    将屏幕比例转换为笔尖触点相对新机械原点的物理坐标。

    X 轴跨手机左/右边，所以左边是 X 小值、右边是 X 大值。
    Y 轴跨手机上/下边，所以上边是 Y 小值、下边是 Y 大值。
    """
    screen_x = clamp01(screen_x)
    screen_y = clamp01(screen_y)
    touch_x_mm = float(d_left) + screen_x * (float(d_right) - float(d_left))
    touch_y_mm = float(d_top) + screen_y * (float(d_bottom) - float(d_top))
    return touch_x_mm, touch_y_mm


def touch_mm_to_motor_ratio(touch_x_mm, touch_y_mm,
                            travel_x, travel_y, offset_x, offset_y):
    """
    将笔尖物理目标转换为电机比例；返回值尚未进行安全边距限制。

    v6 正常配置的 offset_x/offset_y 均为 0；保留参数是为了让纯数学函数
    能显式处理并测试偏移换算。
    """
    motor_x_ratio = ((float(touch_x_mm) - float(offset_x)) / float(travel_x)
                     if touch_x_mm is not None and travel_x > 0 else None)
    motor_y_ratio = ((float(touch_y_mm) - float(offset_y)) / float(travel_y)
                     if touch_y_mm is not None and travel_y > 0 else None)
    return motor_x_ratio, motor_y_ratio


def axis_geometry_valid(near_edge, far_edge, touch_offset, travel):
    """
    检查一条轴上的手机两边是否有序且位于笔尖可达范围。

    电机比例 0~1 对应的笔尖物理范围是
    [touch_offset, touch_offset + travel]。
    """
    if travel <= 0 or touch_offset < 0:
        return False
    if near_edge < touch_offset or far_edge <= near_edge:
        return False
    return far_edge <= touch_offset + travel


def normalize_swipe_direction(value):
    """规范化模型输出的滑动方向；无效值返回空字符串。"""
    direction = str(value or "").strip().lower()
    aliases = {
        "upward": "up",
        "downward": "down",
        "leftward": "left",
        "rightward": "right",
    }
    direction = aliases.get(direction, direction)
    return direction if direction in ("up", "down", "left", "right") else ""


def infer_swipe_direction(x1, y1, x2, y2):
    """按标准图像坐标，从两点位移推断主滑动方向。"""
    dx = float(x2) - float(x1)
    dy = float(y2) - float(y1)
    if abs(dx) < 1e-9 and abs(dy) < 1e-9:
        return ""
    if abs(dx) > abs(dy):
        return "right" if dx > 0 else "left"
    return "down" if dy > 0 else "up"


def swipe_screen_path(direction, start_ratio=0.80, end_ratio=0.25):
    """
    将方向动作转换为屏幕比例坐标中的标准安全手势。

    up 表示触摸笔从屏幕下部移动到上部；其他方向以此类推。
    """
    direction = normalize_swipe_direction(direction)
    start_ratio = clamp01(start_ratio)
    end_ratio = clamp01(end_ratio)
    center = 0.5
    if direction == "up":
        return (center, start_ratio), (center, end_ratio)
    if direction == "down":
        return (center, end_ratio), (center, start_ratio)
    if direction == "left":
        return (start_ratio, center), (end_ratio, center)
    if direction == "right":
        return (end_ratio, center), (start_ratio, center)
    return None
