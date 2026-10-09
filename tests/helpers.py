import io

from PIL import Image


def image_bytes(color="white", format="PNG"):
    output = io.BytesIO()
    Image.new("RGB", (24, 24), color).save(output, format=format)
    return output.getvalue()
