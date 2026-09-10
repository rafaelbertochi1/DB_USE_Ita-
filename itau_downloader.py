"""
Baixa em massa os laudos de avaliação da Inspectos (cliente Itaú) pra
um período de datas, salvando em data/laudos/ pro itau_extractor.py
processar depois.

Uso: pip install playwright && playwright install chromium && python itau_downloader.py

Login: usa sessão salva (sem pedir e-mail/senha). Se expirar, pausa e
pede login manual uma vez.

Primeiro coleta a lista de pendentes do período todo, depois baixa tudo
em paralelo com PARALELISMO abas. Testa com período pequeno antes de
rodar um ano inteiro assim.

Este robô é uma adaptação direta do robô já usado pra baixar laudos do
Santander na mesma Inspectos (mesmo login, mesma navegação) - a única
mudança estrutural é qual card é selecionado na tela "escolha o
cliente". A etapa de download em si (texto dos botões/menus depois de
abrir um laudo) ainda não foi validada ao vivo contra o cliente Itaú -
rode primeiro com HEADLESS = False e um período pequeno pra conferir se
os seletores abaixo (sobretudo em `baixar_um_laudo`) batem com o que
aparece na tela; ajuste se algum texto for diferente do Santander.
"""

import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

LOGIN_URL = "https://inspectos.com/sistema/index.html#/home"
PASTA_SCRIPT = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.path.join(PASTA_SCRIPT, "data", "laudos")
# cookie de sessão da Inspectos - o navegador descarta ao fechar, então
# salvamos/recarregamos na mão. Nunca sobe pro GitHub (.gitignore).
# Arquivo separado do usado pelo robô do Santander, mesmo que rodem na
# mesma máquina - assim um não sobrescreve a sessão do outro.
SESSION_FILE = os.path.join(PASTA_SCRIPT, "sessao_inspectos_itau.json")
LOG_DIR = os.path.join(PASTA_SCRIPT, "logs")

# quantos laudos novos processar nesta execução (cada página tem só 6).
# 9999 = pega tudo do período.
LIMITE_TESTE = 9999

# abas simultâneas baixando em paralelo. Começa mais conservador (3) até
# confirmar que a Inspectos não bloqueia/mostra captcha pro cliente Itaú -
# o robô do Santander subiu pra 6 depois de validar em produção.
PARALELISMO = 3

MODO_RAPIDO = True  # remove a pausa artificial entre cliques

# mantém a janela do Chrome visível em toda execução (por escolha, pra
# poder acompanhar o robô agindo) - o login e a seleção de cliente
# continuam automáticos, mas você vê a tela e pode intervir se algo
# não bater com o esperado. Ainda dá pra rodar sem tela (mais rápido,
# sem janela) mudando pra True aqui.
HEADLESS = False

# cada linha da lista é uma div (não uma <table> de verdade), confirmado
# via Inspecionar elemento no robô do Santander. A tela tem abas
# escondidas com as mesmas linhas no HTML, por isso o :visible no final.
LINHA_SELECTOR = "div.insp360-mouse-link.insp360-tabela-relatorio.insp360-cor-tabela-rel:visible"


class Tee:
    """Escreve simultaneamente no terminal e num arquivo de log."""

    def __init__(self, *streams):
        self.streams = streams

    def write(self, dado):
        for s in self.streams:
            s.write(dado)

    def flush(self):
        for s in self.streams:
            s.flush()


def extrair_numero_proposta(texto_linha):
    """Tenta achar o N° de Proposta dentro do texto de uma linha da tabela."""
    match = re.search(r'\b(\d{6,10})\b', texto_linha)
    return match.group(1) if match else None


def pedir_dado(pergunta):
    """Pede um dado ao usuário com um aviso visual bem claro."""
    print("\n" + "-" * 60)
    print(">>> PRECISO DE UMA INFORMAÇÃO SUA <<<")
    return input(f"{pergunta}: ").strip()


def pausar_para_usuario(*linhas_instrucao):
    """Pausa o robô e deixa bem claro que é a vez do usuário agir."""
    print("\n" + "#" * 60)
    print("#  A AÇÃO É SUA AGORA - O ROBÔ ESTÁ PAUSADO")
    print("#" * 60)
    for linha in linhas_instrucao:
        print(linha)
    input(">>> Quando terminar, clique aqui no terminal e pressione ENTER... ")
    print("#" * 60 + "\n")


def novo_contexto_pagina(browser, com_sessao):
    # viewport grande em ambos os modos - a tabela usa rolagem virtual e
    # só desenha as linhas que cabem na altura visível
    context = browser.new_context(
        accept_downloads=True,
        viewport={"width": 1920, "height": 1080} if HEADLESS else None,
        no_viewport=None if HEADLESS else True,
        storage_state=SESSION_FILE if com_sessao else None,
    )
    return context, context.new_page()


def chegou_no_painel(page, timeout=8000):
    try:
        page.wait_for_selector("text=GRID DE INSPEÇÃO", timeout=timeout)
        return True
    except PlaywrightTimeout:
        return False


def selecionar_cliente_itau(page, timeout=6000):
    # tela "escolha o cliente" aparece mesmo com sessão válida - o logo é
    # imagem, sem texto, então usamos a ordem dos cards: Itaú é o 1º
    # card, Santander o 2º (confirmado no robô que já baixa laudos do
    # Santander nesta mesma Inspectos).
    selecionar = page.locator("text=SELECIONAR")
    try:
        selecionar.first.wait_for(timeout=timeout)
    except PlaywrightTimeout:
        return False

    if selecionar.count() < 1:
        print("      [AVISO] Tela de cliente com layout inesperado - selecione manualmente.")
        return False

    selecionar.nth(0).click()
    page.wait_for_timeout(500)
    print("      Cliente Itaú selecionado automaticamente.")
    return True


def preencher_periodo_e_exibir(page, data_inicio, data_fim):
    page.wait_for_selector("text=GRID DE INSPEÇÃO", timeout=20000)
    page.locator("button.hamburger").click()
    page.click("text=Administrativo")
    page.click("text=Relatórios")
    page.click("text=Inspeções")
    page.click("text=Analítico")

    page.wait_for_selector("text=PERÍODO DE SOLICITAÇÃO DA INSPEÇÃO", timeout=15000)
    # busca os campos de data DEPOIS desse título - o painel FILTROS tem
    # outros campos com a mesma classe CSS
    titulo_periodo = page.locator("text=PERÍODO DE SOLICITAÇÃO DA INSPEÇÃO")
    campos_data = titulo_periodo.locator(
        "xpath=following::input[contains(@class,'insp360-filtro-data')]"
    )

    def preencher_data(campo, valor):
        # .fill() não funciona com a máscara desse campo
        campo.click()
        campo.press("Control+A")
        campo.type(valor, delay=50)
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)

    preencher_data(campos_data.nth(0), data_inicio)
    preencher_data(campos_data.nth(1), data_fim)
    # os botões "30/60/90" ao lado são atalhos de período ("últimos N
    # dias"), não itens por página - não mexer, sobrescrevem as datas

    page.get_by_role("button", name="Exibir").click()
    page.wait_for_selector(LINHA_SELECTOR, timeout=20000)


def baixar_um_laudo(page, laudo_id, destino):
    """Baixa o laudo de avaliação da linha já visível. Retorna 'baixado' ou
    'sem_laudo'; levanta exceção em caso de erro.

    ATENÇÃO: o texto dos botões/menus abaixo ("Laudos", "Download",
    "Laudo Completo"/"DOWNLOAD DE LAUDO") foi copiado do fluxo já
    validado pro Santander. Pro Itaú, o laudo se chama "LAUDO DE
    AVALIAÇÃO" (não "Laudo Completo") - é bem possível que o menu de
    download use outro texto aqui. Rode uma vez com HEADLESS = False
    e confira/ajuste antes de deixar rodando sem supervisão.
    """
    linha = page.locator(LINHA_SELECTOR, has_text=laudo_id)
    if linha.count() == 0:
        raise RuntimeError(f"Não achei mais a linha de {laudo_id} na tela.")
    linha.first.click()
    page.wait_for_selector("text=DETALHAR INSPEÇÃO", timeout=15000)
    page.click("text=Laudos")

    # pode não ter laudo publicado ainda - espera qualquer um dos dois sinais
    icone_engrenagem = page.locator("i.fa-cog, i.fa-gear, .fa-cog")
    aviso_sem_laudo = page.locator("text=Nenhum laudo encontrado")
    icone_engrenagem.or_(aviso_sem_laudo).first.wait_for(timeout=15000)

    try:
        if aviso_sem_laudo.count() > 0:
            return "sem_laudo"

        icone_engrenagem.first.click()
        page.click("text=Download")

        page.wait_for_selector("text=DOWNLOAD DE LAUDO", timeout=15000)
        # se o Itaú não tiver a opção "Laudo Completo", troque a linha
        # abaixo pelo nome que aparecer na tela (ex: "Laudo de Avaliação").
        page.click("text=Laudo Completo")

        with page.expect_download(timeout=30000) as download_info:
            page.get_by_role("button", name="Download").click()
        download = download_info.value
        download.save_as(destino)
        return "baixado"
    finally:
        try:
            if page.locator(".modal.in").count() > 0:
                page.locator("i.insp360-icone-fechar").first.click()
                page.wait_for_selector(".modal.in", state="hidden", timeout=15000)
        except Exception:
            pass
        page.wait_for_selector(LINHA_SELECTOR, timeout=15000)


def baixar_lote_paralelo(indice, minha_lista, data_inicio, data_fim, log_lock):
    """Baixa uma fatia dos pendentes numa aba própria, em paralelo com as
    outras. Cada aba abre seu próprio navegador Playwright (não
    compartilha `browser` com as outras) - é a forma segura de
    paralelizar com a API síncrona."""
    prefixo = f"  [Aba {indice + 1}]"

    def log(msg):
        with log_lock:
            print(f"{prefixo} {msg}")

    pendentes_meus = {
        laudo_id: (numero_proposta, destino)
        for numero_proposta, laudo_id, destino in minha_lista
    }
    baixados = sem_laudo = erros = 0

    with sync_playwright() as p:
        navegador = p.chromium.launch(
            headless=HEADLESS,
            args=[] if HEADLESS else ["--start-maximized"],
        )
        _contexto, pagina = novo_contexto_pagina(navegador, com_sessao=True)
        try:
            pagina.goto(LOGIN_URL)
            selecionar_cliente_itau(pagina)
            if not chegou_no_painel(pagina, timeout=15000):
                log("[ERRO] Não consegui entrar no painel com a sessão salva - abortando esta aba.")
                return (0, 0, len(pendentes_meus))

            preencher_periodo_e_exibir(pagina, data_inicio, data_fim)

            while pendentes_meus:
                linhas = pagina.locator(LINHA_SELECTOR)
                for i in range(linhas.count()):
                    if not pendentes_meus:
                        break
                    laudo_id = linhas.nth(i).inner_text().split("\n")[0].strip()
                    if laudo_id not in pendentes_meus:
                        continue
                    numero_proposta, destino = pendentes_meus.pop(laudo_id)
                    log(f"Abrindo laudo {laudo_id} (proposta {numero_proposta})...")
                    try:
                        resultado = baixar_um_laudo(pagina, laudo_id, destino)
                        if resultado == "baixado":
                            baixados += 1
                            log(f"   salvo em: {destino}")
                        else:
                            sem_laudo += 1
                            log(f"   [PULADO] Ainda não há laudo publicado para {laudo_id}.")
                    except PlaywrightTimeout as e:
                        erros += 1
                        log(f"   [PULADO - ERRO] {str(e).splitlines()[0]}")
                    except Exception as e:
                        erros += 1
                        log(f"   [PULADO - ERRO INESPERADO] {str(e)}")

                if not pendentes_meus:
                    break

                botao_proxima = pagina.locator("a:visible", has_text="próxima")
                if botao_proxima.count() == 0 or botao_proxima.get_attribute("disabled") is not None:
                    log(
                        f"Cheguei ao fim das páginas com {len(pendentes_meus)} laudo(s) da minha "
                        "lista não encontrados (raro - confira manualmente)."
                    )
                    erros += len(pendentes_meus)
                    break

                botao_proxima.click()
                pagina.wait_for_selector(LINHA_SELECTOR, timeout=15000)
        finally:
            navegador.close()

    return (baixados, sem_laudo, erros)


def main():
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)

    log_path = os.path.join(LOG_DIR, f"execucao_{datetime.now():%Y%m%d_%H%M%S}.txt")
    log_file = open(log_path, "w", encoding="utf-8")
    stdout_original = sys.stdout
    sys.stdout = Tee(stdout_original, log_file)

    inicio = time.time()
    try:
        print("=" * 60)
        print(" ROBÔ DE DOWNLOAD - INSPECTOS (ITAÚ)")
        print("=" * 60)
        print(f"Log desta execução: {log_path}")
        data_inicio = pedir_dado("Data inicial (dd/mm/aaaa)")
        data_fim = pedir_dado("Data final (dd/mm/aaaa)")
        print(f"Período usado: {data_inicio} até {data_fim}")  # input() não vai pro log sozinho

        pendentes = []
        total_pulados_existentes = 0

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=HEADLESS,
                slow_mo=0 if MODO_RAPIDO else 300,
                args=[] if HEADLESS else ["--start-maximized"],
            )
            sessao_existente = os.path.exists(SESSION_FILE)
            context, page = novo_contexto_pagina(browser, com_sessao=sessao_existente)

            def salvar_sessao():
                context.storage_state(path=SESSION_FILE)

            try:
                print("\n[1/4] Verificando sessão salva...")
                page.goto(LOGIN_URL)
                selecionar_cliente_itau(page)
                if sessao_existente and chegou_no_painel(page, timeout=10000):
                    print("      Sessão válida, painel carregado direto.")
                elif HEADLESS:
                    print("\n[ERRO] A sessão salva expirou (ou pediu verificação de novo), e o robô")
                    print("está rodando sem tela (HEADLESS = True) - não dá pra fazer login manual assim.")
                    print(f"Abra o {os.path.basename(__file__)}, mude HEADLESS para False, rode de novo")
                    print("pra refazer o login manualmente uma vez, e depois pode voltar HEADLESS")
                    print("para True - a sessão fica salva de novo.")
                    return
                else:
                    pausar_para_usuario(
                        "A sessão salva não está mais válida (ou pediu login/",
                        "verificação de novo).",
                        "1. Faça o login manualmente na janela do Chrome (e-mail e senha).",
                        "2. Selecione o cliente Itaú, se for pedido (o robô já tenta",
                        "   fazer isso sozinho, mas confirme se ele conseguiu).",
                        "3. Complete a verificação de segurança, se pedir.",
                        "4. Espere até ver a tela com 'GRID DE INSPEÇÃO'.",
                    )
                    if not chegou_no_painel(page, timeout=10000):
                        print("\n[ERRO] Ainda não consegui ver o painel principal (GRID DE INSPEÇÃO).")
                        print("Encerrando esta execução.")
                        return
                    salvar_sessao()
                    print("      Sessão salva para as próximas execuções.")

                print("[2/4] Navegando até Relatório Analítico...")
                print("[3/4] Preenchendo período e clicando em Exibir...")
                preencher_periodo_e_exibir(page, data_inicio, data_fim)

                print("[4/4] Coletando lista de laudos do período (sem baixar ainda)...")
                pagina_num = 1
                while len(pendentes) < LIMITE_TESTE:
                    # dedup pelo código do laudo - numero_proposta pode repetir
                    linhas = page.locator(LINHA_SELECTOR)
                    qtd_linhas = linhas.count()

                    laudos_da_pagina = []
                    codigos_vistos = set()
                    for i in range(qtd_linhas):
                        texto_linha = linhas.nth(i).inner_text()
                        laudo_id = texto_linha.split("\n")[0].strip()
                        if not laudo_id:  # linhas duplicadas de abas escondidas vêm sem código
                            continue
                        if laudo_id in codigos_vistos:
                            continue
                        codigos_vistos.add(laudo_id)
                        numero_proposta = extrair_numero_proposta(texto_linha) or ""
                        laudos_da_pagina.append((numero_proposta, laudo_id))

                    print(f"  Página {pagina_num}: {len(laudos_da_pagina)} laudos únicos encontrados na tela.")

                    for numero_proposta, laudo_id in laudos_da_pagina:
                        destino = os.path.join(DOWNLOAD_DIR, f"laudo_{laudo_id}.pdf")
                        if os.path.exists(destino):
                            total_pulados_existentes += 1
                            continue
                        pendentes.append((numero_proposta, laudo_id, destino))

                    if len(pendentes) >= LIMITE_TESTE:
                        break

                    # abas escondidas têm cada uma seu próprio botão "próxima" no HTML
                    botao_proxima = page.locator("a:visible", has_text="próxima")
                    if botao_proxima.count() == 0:
                        print("  Não há mais páginas.")
                        break
                    if botao_proxima.get_attribute("disabled") is not None:
                        print("  Não há mais páginas.")
                        break
                    botao_proxima.click()
                    pagina_num += 1
                    page.wait_for_selector(LINHA_SELECTOR, timeout=15000)

                pendentes = pendentes[:LIMITE_TESTE]
                print(f"\n  {len(pendentes)} laudo(s) pendente(s) pra baixar, {total_pulados_existentes} já existiam.")

            except PlaywrightTimeout as e:
                print("\n[ERRO] O robô travou esperando um elemento aparecer na tela.")
                print("Copie a mensagem abaixo e me envie, junto com o que estava")
                print("aparecendo na janela do Chrome nesse momento:")
                print(str(e))
                pendentes = []
            except Exception as e:
                print("\n[ERRO INESPERADO]")
                print(str(e))
                pendentes = []
            finally:
                try:
                    salvar_sessao()
                except Exception:
                    pass
                if not HEADLESS:
                    print("\n" + "#" * 60)
                    print("#  A AÇÃO É SUA AGORA - O ROBÔ ESTÁ PAUSADO")
                    print("#" * 60)
                    input(">>> Pressione ENTER aqui para fechar o navegador... ")
                browser.close()

        total_baixados = total_sem_laudo = total_erros = 0
        if pendentes:
            num_workers = min(PARALELISMO, len(pendentes))
            partes = [pendentes[i::num_workers] for i in range(num_workers)]
            print(f"\nBaixando {len(pendentes)} laudo(s) com {num_workers} aba(s) em paralelo...")
            log_lock = threading.Lock()

            with ThreadPoolExecutor(max_workers=num_workers) as executor:
                futuros = [
                    executor.submit(baixar_lote_paralelo, i, parte, data_inicio, data_fim, log_lock)
                    for i, parte in enumerate(partes)
                ]
                for futuro in as_completed(futuros):
                    baixados, sem_laudo, erros = futuro.result()
                    total_baixados += baixados
                    total_sem_laudo += sem_laudo
                    total_erros += erros

        decorrido = time.time() - inicio
        minutos, segundos = divmod(int(decorrido), 60)
        print("\nConcluído!")
        print("-" * 60)
        print(f"  Baixados agora:            {total_baixados}")
        print(f"  Já existiam (pulados):     {total_pulados_existentes}")
        print(f"  Sem laudo publicado ainda: {total_sem_laudo}")
        print(f"  Com erro (pulados):        {total_erros}")
        print(f"  Tempo total:               {minutos}min{segundos:02d}s")
        if total_baixados:
            print(f"  Média por laudo baixado:   {decorrido / total_baixados:.1f}s")
        print(f"  Pasta: {DOWNLOAD_DIR}")
        print("-" * 60)

    finally:
        sys.stdout = stdout_original
        log_file.close()


if __name__ == "__main__":
    main()
