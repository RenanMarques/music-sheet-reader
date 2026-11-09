# Leitor de Partituras

Ferramenta web que lê partituras (PDF, imagem ou MusicXML), identifica as vozes e as reproduz com sons sintetizados, tocando todas juntas ou isoladamente (ex.: só o baixo de um hino a 4 vozes).

## Como rodar

Requer apenas Docker.

```bash
./start.sh          # constrói, inicia e espera a aplicação responder
./start.sh --open   # ...e abre no navegador
```

Use `PORT=8080 ./start.sh` para outra porta e `./start.sh --no-build` para iniciar sem reconstruir. Para parar: `docker compose down`. O container reinicia sozinho junto com o Docker até ser parado.

Abra http://localhost:8000 e envie uma partitura (ex.: `examples/Cantam Anjos Harmonia.pdf`). PDFs e imagens passam por reconhecimento óptico ([Audiveris](https://github.com/Audiveris/audiveris)), o que leva de ~30 s a alguns minutos por arquivo.

## Recursos

- Detecta o número de vozes por pauta e as separa (SATB recebe os nomes Soprano/Contralto/Tenor/Baixo).
- Solo, mudo e volume por voz; botões “Só Baixo” etc.
- Andamento ajustável, timbres, repetição, cursor acompanhando a partitura.
- Download do MusicXML reconhecido. O OMR pode errar notas e ritmos; corrija em um editor (ex.: MuseScore) e reenvie o `.musicxml`.

## Testes

```bash
docker run --rm -v "$PWD/backend:/app/backend" -w /app/backend music-sheet-reader python -m pytest -q
```
