FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# ── Bibliothèques SIG système ────────────────────────────────────────────────
# GeoDjango (django.contrib.gis) charge libgdal / libgeos_c / libproj via ctypes.
# gdal-bin + libgdal-dev fournissent aussi les en-têtes pour compiler le binding
# Python GDAL à la version EXACTE de la lib système (pip install GDAL==...).
# g++/binutils : compilation du binding ; proj-data : grilles de transformation.
RUN apt-get update && apt-get install -y --no-install-recommends \
      binutils g++ \
      gdal-bin libgdal-dev \
      libgeos-dev \
      libproj-dev proj-bin proj-data \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && pip install --no-cache-dir "GDAL==$(gdal-config --version)"

COPY . .
EXPOSE 8000
CMD ["sh", "-c", "python manage.py migrate --no-input && python manage.py runserver 0.0.0.0:8000"]
