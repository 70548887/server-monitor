import random
import io
import base64
import uuid
from PIL import Image, ImageDraw, ImageFilter

class SliderCaptcha:
    """
    简单的滑动验证码生成器
    """
    def __init__(self, width=310, height=155, slider_size=50):
        self.width = width
        self.height = height
        self.slider_size = slider_size

    def generate(self):
        """
        生成验证码图片和位置信息
        """
        # 尝试从本地静态库随机读取一张图片作为背景
        bg = self._load_local_bg()
        if not bg:
            # 如果本地没有图片，则降级为随机生成
            bg = self._create_random_bg()
        
        # 随机生成滑块位置 (避开边缘)
        x = random.randint(self.slider_size + 20, self.width - self.slider_size - 20)
        y = random.randint(20, self.height - self.slider_size - 20)
        
        # 创建滑块图片
        slider = Image.new('RGBA', (self.slider_size, self.slider_size), (0, 0, 0, 0))
        # 从背景裁剪出滑块内容
        slice_obj = bg.crop((x, y, x + self.slider_size, y + self.slider_size))
        
        # 为滑块添加一点边框或阴影效果 (简单处理)
        slider.paste(slice_obj, (0, 0))
        
        # 在原图上把滑块位置弄暗/模糊
        draw = ImageDraw.Draw(bg)
        draw.rectangle([x, y, x + self.slider_size, y + self.slider_size], fill=(0, 0, 0, 100))
        
        # 转换为 base64
        bg_base64 = self._image_to_base64(bg)
        slider_base64 = self._image_to_base64(slider)
        
        return {
            "bg": bg_base64,
            "slider": slider_base64,
            "y": y,
            "x": x  # 这个值不应该发给前端，仅用于验证
        }

    def _load_local_bg(self):
        """尝试从本地读取背景图"""
        import os
        bg_dir = os.path.join("app", "static", "captcha_bg")
        if os.path.exists(bg_dir):
            files = [f for f in os.listdir(bg_dir) if f.endswith(('.png', '.jpg', '.jpeg'))]
            if files:
                picked = random.choice(files)
                try:
                    img = Image.open(os.path.join(bg_dir, picked))
                    # 确保尺寸一致
                    if img.size != (self.width, self.height):
                        img = img.resize((self.width, self.height), Image.Resampling.LANCZOS)
                    return img.convert('RGB')
                except Exception:
                    return None
        return None

    def _create_random_bg(self):
        """生成随机背景"""
        bg = Image.new('RGB', (self.width, self.height), (
            random.randint(200, 255), 
            random.randint(200, 255), 
            random.randint(200, 255)
        ))
        draw = ImageDraw.Draw(bg)
        # 画一些随机色块或线条
        for _ in range(10):
            x1 = random.randint(0, self.width)
            y1 = random.randint(0, self.height)
            x2 = random.randint(0, self.width)
            y2 = random.randint(0, self.height)
            draw.line([x1, y1, x2, y2], fill=(
                random.randint(100, 200),
                random.randint(100, 200),
                random.randint(100, 200)
            ), width=random.randint(1, 5))
            
        return bg

    def _image_to_base64(self, img):
        buffered = io.BytesIO()
        img.save(buffered, format="PNG")
        return base64.b64encode(buffered.getvalue()).decode('utf-8')

# 内存中简易存储验证码结果 (实际项目建议用 Redis)
captcha_store = {}

async def create_captcha():
    sc = SliderCaptcha()
    data = sc.generate()
    captcha_id = str(uuid.uuid4())
    # 存储正确答案
    captcha_store[captcha_id] = data['x']
    
    # 返回给前端的数据不包含 x
    return {
        "id": captcha_id,
        "bg": f"data:image/png;base64,{data['bg']}",
        "slider": f"data:image/png;base64,{data['slider']}",
        "y": data['y']
    }

async def verify_captcha(captcha_id, x_offset):
    if captcha_id not in captcha_store:
        return False
    
    correct_x = captcha_store.pop(captcha_id)
    # 允许 5 像素的误差
    return abs(correct_x - x_offset) <= 5
