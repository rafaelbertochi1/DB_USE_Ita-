@echo off
REM Abre o Docker Desktop (se nao estiver aberto), espera ele ficar pronto,
REM e sobe o Postgres + Adminer compartilhado (o mesmo banco usado pelo
REM Automatiza-o-Uono e por este projeto).
REM
REM Se a busca abaixo nao achar a pasta do Automatiza-o-Uono na sua
REM maquina, so editar a linha BASE_GITHUB.

setlocal

set "BASE_GITHUB=%USERPROFILE%\Documents\GitHub"
set "DOCKER_DESKTOP_EXE=C:\Program Files\Docker\Docker\Docker Desktop.exe"

REM acha a pasta do Automatiza-o-Uono por padrao (o nome exato pode
REM variar - "Automatiza-o-Uono", "Automatiza-o-Uono-limpo" etc.) em vez
REM de exigir o nome certinho.
set "PASTA_AUTOMATIZA="
for /d %%A in ("%BASE_GITHUB%\Automatiza*") do set "PASTA_AUTOMATIZA=%%A"

REM a pasta "Backend l Script Extração Laudos" tem acento no nome, o que
REM costuma embaralhar em .bat (aparece tipo "Extra├º├úo") dependendo da
REM codificação do arquivo - em vez de digitar o nome acentuado, acha a
REM pasta por um pedaço só em ASCII (funciona mesmo se o acento
REM aparecer errado aqui).
set "PASTA_DOCKER_COMPOSE="
if defined PASTA_AUTOMATIZA for /d %%D in ("%PASTA_AUTOMATIZA%\Backend*") do set "PASTA_DOCKER_COMPOSE=%%D"

echo ============================================================
echo  Subindo o banco (Postgres + Adminer)
echo ============================================================

REM --- 1. Verifica se o Docker ja esta rodando ---
docker info >nul 2>&1
if not errorlevel 1 goto docker_pronto

echo [1/3] Docker Desktop nao esta rodando - abrindo...
if not exist "%DOCKER_DESKTOP_EXE%" (
    echo.
    echo [ERRO] Nao encontrei o Docker Desktop em:
    echo   %DOCKER_DESKTOP_EXE%
    echo Abra o Docker Desktop manualmente e rode este arquivo de novo,
    echo ou edite a linha DOCKER_DESKTOP_EXE no topo deste .bat com o
    echo caminho certo na sua maquina.
    pause
    exit /b 1
)
start "" "%DOCKER_DESKTOP_EXE%"

echo       Aguardando o Docker iniciar (pode levar um tempo na primeira vez)...
:esperar_docker
timeout /t 3 >nul
docker info >nul 2>&1
if errorlevel 1 goto esperar_docker

:docker_pronto
echo [2/3] Docker pronto.

REM --- 2. Sobe o Postgres + Adminer compartilhado ---
if not defined PASTA_AUTOMATIZA (
    echo.
    echo [ERRO] Nao encontrei nenhuma pasta comecando com "Automatiza" em:
    echo   %BASE_GITHUB%
    echo Edite a linha BASE_GITHUB no topo deste .bat com o caminho onde
    echo fica a pasta do GitHub na sua maquina.
    pause
    exit /b 1
)
if not defined PASTA_DOCKER_COMPOSE (
    echo.
    echo [ERRO] Nao encontrei a pasta "Backend..." dentro de:
    echo   %PASTA_AUTOMATIZA%
    pause
    exit /b 1
)
if not exist "%PASTA_DOCKER_COMPOSE%\docker-compose.yml" (
    echo.
    echo [ERRO] Nao encontrei o docker-compose.yml em:
    echo   %PASTA_DOCKER_COMPOSE%
    pause
    exit /b 1
)

echo [3/3] Subindo os containers (postgres_pdf + adminer_pdf)...
pushd "%PASTA_DOCKER_COMPOSE%"
docker compose up -d
popd

echo.
echo ============================================================
echo  Pronto!
echo ============================================================
echo  Banco (Postgres): localhost:5432  (usuario/senha: postgres/postgres, banco: testdb)
echo  Adminer (interface web): http://localhost:8080
echo ============================================================
pause
