import re
from PIL import ImageDraw, ImageOps
from .contracts import Image, ToolResult
from .errors import InvalidParams


def known_fields(params, names):
    for field in params:
        if field not in names:
            raise InvalidParams(field, 'Unknown parameter')


class FrameTool:
    name = 'frame'

    def validate(self, params):
        known_fields(params, {'style', 'width', 'color'})
        if params.get('style', 'classic') not in ('thin', 'classic'):
            raise InvalidParams('style', 'Expected thin or classic')
        width = params.get('width', 5)
        if type(width) is not int or not 1 <= width <= 15:
            raise InvalidParams('width', 'Must be an integer from 1 to 15')
        color = params.get('color', '#FFFFFF')
        if not isinstance(color, str) or not re.fullmatch(r'#[0-9a-fA-F]{6}', color):
            raise InvalidParams('color', 'Expected #RRGGBB')

    def run(self, image, params, ctx):
        pixels = image.pixels()
        width = max(1, round(min(pixels.size) * params.get('width', 5) / 100))
        draw = ImageDraw.Draw(pixels)
        color = params.get('color', '#FFFFFF')
        if params.get('style', 'classic') == 'thin':
            inset = min(width, (min(pixels.size) - 1) // 2)
            draw.rectangle((inset, inset, pixels.width - 1 - inset, pixels.height - 1 - inset), outline=color)
        else:
            draw.rectangle((0, 0, pixels.width - 1, pixels.height - 1), outline=color, width=width)
        return ToolResult.ok(Image(pixels))


class FilterTool:
    name = 'filter'

    def validate(self, params):
        known_fields(params, {'name'})
        if params.get('name') not in ('sepia', 'grayscale'):
            raise InvalidParams('name', 'Expected sepia or grayscale')

    def run(self, image, params, ctx):
        gray = ImageOps.grayscale(image.pixels())
        pixels = gray.convert('RGB') if params['name'] == 'grayscale' else ImageOps.colorize(gray, '#000000', '#FFF0C0')
        return ToolResult.ok(Image(pixels))
