#!/usr/bin/env python3
"""
Скрипт для проверки зависимостей FastAPI приложения
Запускается перед деплоем на Render для проверки готовности
"""

import sys
import importlib

def check_dependency(package_name, import_name=None):
    """Проверяет, установлена ли зависимость"""
    if import_name is None:
        import_name = package_name
    
    try:
        importlib.import_module(import_name)
        print(f"✅ {package_name} - OK")
        return True
    except ImportError:
        print(f"❌ {package_name} - MISSING")
        return False

def main():
    """Проверяет все необходимые зависимости"""
    print("🔍 Проверка зависимостей FastAPI приложения...\n")
    
    required_packages = [
        ("fastapi", "fastapi"),
        ("uvicorn", "uvicorn"),
        ("jinja2", "jinja2"),
        ("python-multipart", "multipart"),
        ("starlette", "starlette"),
        ("aiofiles", "aiofiles"),
        ("pandas", "pandas"),
        ("numpy", "numpy"),
        ("scikit-learn", "sklearn"),
        ("joblib", "joblib"),
        ("catboost", "catboost")
    ]
    
    missing_packages = []
    
    for package, import_name in required_packages:
        if not check_dependency(package, import_name):
            missing_packages.append(package)
    
    print(f"\n📊 Результат проверки:")
    if missing_packages:
        print(f"❌ Отсутствуют зависимости: {', '.join(missing_packages)}")
        print("\n💡 Для установки выполните:")
        print("pip install -r requirements.txt")
        return 1
    else:
        print("✅ Все зависимости установлены корректно!")
        print("\n🚀 Приложение готово к деплою на Render")
        return 0

if __name__ == "__main__":
    sys.exit(main())