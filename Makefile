# Atajos del experimento. Todo lo que produce números pasa por Docker.
SHELL := /bin/bash
IMAGE := tfg
DATA  := data/online_retail_II.csv
MOUNT := -v "$(PWD)/data:/app/data:ro" -v "$(PWD)/results:/app/results" \
         -v "$(PWD)/latex:/app/latex"

.PHONY: help dataset build matriz analizar smoke limpiar

help:
	@echo "make dataset   baja Online Retail II de UCI y arma el CSV (pandas + openpyxl en el host)"
	@echo "make build     construye la imagen de medicion"
	@echo "make matriz    105 corridas -> results/raw_runs.csv y results/poblacion.json"
	@echo "make analizar  tablas, figuras y metricas -> latex/generado/"
	@echo "make smoke     una repeticion con 4000 filas -> results/_smoke/ (sin valor de medicion)"

dataset: $(DATA)

$(DATA):
	@mkdir -p data
	curl -sSL -o data/online_retail_II.zip \
	  "https://archive.ics.uci.edu/static/public/502/online+retail+ii.zip"
	cd data && unzip -o online_retail_II.zip && rm -f online_retail_II.zip
	python3 -c "import pandas as pd; \
	  xl = pd.ExcelFile('data/online_retail_II.xlsx'); \
	  pd.concat([xl.parse(s) for s in xl.sheet_names], ignore_index=True) \
	    .to_csv('$(DATA)', index=False)"
	@echo "OK -> $(DATA)"

build:
	docker build -t $(IMAGE) .

matriz: build $(DATA)
	docker run --rm $(MOUNT) $(IMAGE)

analizar: build
	docker run --rm $(MOUNT) $(IMAGE) python -m src.analyze

smoke: build $(DATA)
	docker run --rm $(MOUNT) $(IMAGE) python -m src.runner --reps 1 --rows 4000 \
	  --no-warmup --out results/_smoke/raw_runs.csv

limpiar:
	rm -rf src/__pycache__ results/_smoke*
