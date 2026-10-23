# Entorno de medición con versiones fijadas. Justificación metodológica: un
# entorno de cómputo compartido y elástico (serverless) no permite que un
# tercero reproduzca los tiempos; el contenedor sí.
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    openjdk-21-jre-headless procps && rm -rf /var/lib/apt/lists/*
# JAVA_HOME independiente de la arquitectura (amd64 / arm64)
RUN ln -s "$(dirname "$(dirname "$(readlink -f "$(which java)")")")" /usr/lib/jvm/default
ENV JAVA_HOME=/usr/lib/jvm/default
ENV PATH="${JAVA_HOME}/bin:${PATH}"
ENV PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Descarga de los JARs de Delta durante el build: una sola vez, queda en la
# imagen y la medición no incluye descargas de red. Solo depende de core.py,
# así que editar el resto de src/ no la repite.
COPY src/core.py ./src/core.py
RUN python -c "from src.core import get_spark; get_spark('/tmp/wh').stop()" \
 && rm -rf /tmp/wh

COPY src/ ./src/
COPY contracts/ ./contracts/

# data/ y results/ se montan como volúmenes (ver Makefile). Los valores por
# defecto del runner son la configuración de la corrida del paper.
CMD ["python", "-m", "src.runner"]
