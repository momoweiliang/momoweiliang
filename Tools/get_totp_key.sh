#!/bin/bash

# ------------------------------
# 从二维码图片中提取 TOTP 密钥
# 适用于 macOS，依赖 zbarimg 工具
# ------------------------------

# 使用说明函数
show_usage() {
  echo ""
  echo "🔐 从二维码图片中提取 TOTP 密钥"
  echo ""
  echo "用法："
  echo "  ./get_totp_key.sh <二维码图片路径>"
  echo ""
  echo "参数："
  echo "  <二维码图片路径>   二维码图片文件（如 PNG、JPG）的完整路径"
  echo ""
  echo "示例："
  echo "  ./get_totp_key.sh ~/Desktop/totp_qr.png"
  echo ""
  echo "依赖项："
  echo "  本脚本依赖 zbar 工具，请使用以下命令安装："
  echo "    brew install zbar"
  echo ""
}

# 如果未传入参数，显示使用说明并退出
if [ -z "$1" ]; then
  echo "❌ 错误：未提供二维码图片文件路径。"
  show_usage
  exit 1
fi

# 读取图片文件路径
IMAGE_FILE="$1"

# 检查文件是否存在
if [ ! -f "$IMAGE_FILE" ]; then
  echo "❌ 错误：文件 $IMAGE_FILE 不存在。"
  exit 1
fi

# 检查是否安装 zbarimg
if ! command -v zbarimg >/dev/null 2>&1; then
  echo "❌ 错误：未找到 zbarimg 工具。请先安装 zbar："
  echo "brew install zbar"
  exit 1
fi

# 使用 zbarimg 解析二维码内容
QR_CONTENT=$(zbarimg --quiet "$IMAGE_FILE" 2>/dev/null)

# 如果二维码内容为空
if [ -z "$QR_CONTENT" ]; then
  echo "❌ 错误：无法从图片中解析二维码内容。"
  exit 1
fi

# 提取 otpauth URL（TOTP 格式）
TOTP_URL=$(echo "$QR_CONTENT" | grep -o 'otpauth://[^ ]*')

# 检查是否成功提取到 TOTP URL
if [ -n "$TOTP_URL" ]; then
  echo "✅ 成功解析 TOTP URL："
  echo "$TOTP_URL"
else
  echo "❌ 错误：二维码内容中未找到有效的 TOTP 信息（otpauth://）。"
  exit 1
fi

