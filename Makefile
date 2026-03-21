PROJECT = 5i

.PHONY: run setup push

run:
	python3 app.py

setup:
	python3 -m pip install -r requirements.txt
	cp -n .env.example .env || true

push:
	git add .
	git commit -m "$(m)"
	git push origin main
