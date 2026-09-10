import os
import re
import multiprocessing
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from decimal import Decimal, InvalidOperation
import pdfplumber
import psycopg2
from psycopg2.extras import execute_values

ZERO = Decimal('0.00')

_MESES_PT = {
    "janeiro": 1, "fevereiro": 2, "março": 3, "marco": 3, "abril": 4,
    "maio": 5, "junho": 6, "julho": 7, "agosto": 8, "setembro": 9,
    "outubro": 10, "novembro": 11, "dezembro": 12,
}


def converter_float_seguro(val):
    # Decimal em vez de float pra não gerar sobra de binário
    # (8666.299999999999 em vez de 8666.30) nas colunas NUMERIC.
    if val is None:
        return ZERO
    if isinstance(val, Decimal):
        return val.quantize(Decimal('0.01'))
    if isinstance(val, (int, float)):
        try:
            return Decimal(str(val)).quantize(Decimal('0.01'))
        except InvalidOperation:
            return ZERO

    s_val = str(val).strip()
    if not s_val or s_val.upper() in ['-', '--', '—', 'NONE', 'NULL']:
        return ZERO

    match = re.search(r'[-+]?[\d\.,]+', s_val)
    if not match:
        return ZERO

    num_str = match.group(0)

    if '.' in num_str and ',' in num_str:
        num_str = num_str.replace('.', '').replace(',', '.')
    elif '.' in num_str and ',' not in num_str:
        partes = num_str.split('.')
        if len(partes) == 2 and len(partes[1]) == 3:
            num_str = num_str.replace('.', '')
        elif len(partes) > 2:
            num_str = num_str.replace('.', '')
    elif ',' in num_str:
        num_str = num_str.replace(',', '.')

    try:
        return Decimal(num_str).quantize(Decimal('0.01'))
    except InvalidOperation:
        return ZERO


def converter_int_seguro(val):
    try:
        return int(converter_float_seguro(val))
    except Exception:
        return 0


def converter_float_coordenada(val):
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    try:
        return float(str(val).strip())
    except Exception:
        return None


def limpar_txt(val, valor_padrao=""):
    if not val:
        return valor_padrao
    txt = str(val).strip()
    return txt if txt and txt.upper() != "NULL" else valor_padrao


def _linha_e_indice_coluna(text, label_regex):
    """O relatório da Inspectos pro Itaú é uma tabela de duas colunas onde
    o pdfplumber devolve 'NN - Rótulo A   MM - Rótulo B\\nvalor A   valor B'
    - cada linha de cabeçalho tem 1 ou 2 campos, e a linha seguinte tem os
    valores dos dois na mesma ordem. A numeração (NN/MM) muda entre o
    Laudo Físico e o Eletrônico (e entre Casa/Apartamento), então em vez
    de fixar os números como no extractor do Santander, acha o rótulo
    pelo texto e conta quantos rótulos "NN - " vêm antes dele na mesma
    linha de cabeçalho pra saber se o valor dele é o 1º ou o 2º token da
    linha de valores seguinte.
    """
    m = re.search(label_regex, text, re.IGNORECASE)
    if not m:
        return None, None
    inicio_linha = text.rfind('\n', 0, m.start()) + 1
    fim_cabecalho = text.find('\n', m.end())
    if fim_cabecalho == -1:
        return None, None
    texto_antes_na_linha = text[inicio_linha:m.start()]
    # -1 porque o próprio rótulo também tem um número na frente ("NN - ",
    # já contado aqui) - só sobra 1 a mais no texto_antes_na_linha por
    # cada rótulo IRMÃO que vier antes dele na mesma linha.
    indice_coluna = max(0, len(re.findall(r'\d+\s*-\s*', texto_antes_na_linha)) - 1)
    fim_valores = text.find('\n', fim_cabecalho + 1)
    if fim_valores == -1:
        fim_valores = len(text)
    linha_valores = text[fim_cabecalho + 1:fim_valores]
    return linha_valores.split(), indice_coluna


def _campo_numerico(text, label_regex, tipo='decimal'):
    tokens, indice = _linha_e_indice_coluna(text, label_regex)
    if not tokens or indice is None or indice >= len(tokens):
        return None
    token = tokens[indice]
    pat = r'\d+,\d{1,2}' if tipo == 'decimal' else r'\d+'
    m = re.search(pat, token)
    return m.group(0) if m else None


def _campo_texto(text, label_regex):
    tokens, indice = _linha_e_indice_coluna(text, label_regex)
    if not tokens or indice is None or indice >= len(tokens):
        return None
    return tokens[indice]


def extrair_codigo_laudo(text):
    m = re.search(r'#([A-Z]{2,6}\d+)', text)
    return m.group(1) if m else None


def extrair_numero_proposta_e_tipo_laudo(text):
    m = re.search(
        r'Solicitante\s+N[°ºo]\s*da\s*Proposta\s+Tipo\s+de\s+Laudo\n\S+\s+(\d+)\s+(Laudo\s+\S+)',
        text, re.IGNORECASE
    )
    if m:
        return m.group(1), m.group(2).strip()
    return "", ""


def extrair_endereco_numero_complemento(text):
    m = re.search(
        r'Endere[çc]o\s+N[uú]mero\s+Complemento\n(.+?)\s+(\d+|S/N)\s*(.*)',
        text, re.IGNORECASE
    )
    if m:
        return limpar_txt(m.group(1)), limpar_txt(m.group(2), "S/N"), limpar_txt(m.group(3))
    return "", "S/N", ""


def extrair_coordenadas(text):
    m = re.search(
        r'Coordenadas\s+do\s+Im[óo]vel[^\n]*\n\s*(-?\d+\.\d+),\s*(-?\d+\.\d+)',
        text, re.IGNORECASE
    )
    if not m:
        return None, None, None
    lat = converter_float_coordenada(m.group(1))
    lon = converter_float_coordenada(m.group(2))
    if lat is None or lon is None:
        return None, None, None
    return f"{lat}, {lon}", lat, lon


def extrair_data_vistoria(text):
    m = re.search(r'Data\s+da\s+Vistoria[^\n]*\n([^\n]+)', text, re.IGNORECASE)
    if m:
        data_m = re.search(r'(\d{2}/\d{2}/\d{4})', m.group(1))
        if data_m:
            return data_m.group(1)

    # Laudo Eletrônico não tem seção "DADOS DA VISTORIA" - usa a data de
    # assinatura do laudo como aproximação ("...-feira, 1 de Setembro de 2026").
    m = re.search(
        r'-feira,\s*(\d{1,2})\s+de\s+([A-Za-zÀ-ÿ]+)\s+de\s+(\d{4})',
        text, re.IGNORECASE
    )
    if m:
        dia, mes_nome, ano = m.groups()
        mes = _MESES_PT.get(mes_nome.lower())
        if mes:
            return f"{int(dia):02d}/{mes:02d}/{ano}"
    return None


def extrair_valor_avaliacao(text):
    m = re.search(
        r'VALOR\s+DA\s+AVALIA[ÇC][ÃA]O\s*\n\s*R?\$?\s*([\d\.,]+)',
        text, re.IGNORECASE
    )
    return m.group(1) if m else None


def extrair_valor_unitario_m2(text):
    m = re.search(
        r'[ÁA]rea\s+constru[íi]da\s*\(m[²2]\)\s+Valor\s+unit[áa]rio\s*\(R\$/m[²2]\)\s+Valor\s+parcial\s*\(R\$\)'
        r'\n[\d\.,]+\s+R\$\s*([\d\.,]+)',
        text, re.IGNORECASE
    )
    return m.group(1) if m else None


def extrair_dados_pdf(pdf_path):
    try:
        file_name = os.path.basename(pdf_path)
        with pdfplumber.open(pdf_path) as pdf:
            full_text = ""
            for page in pdf.pages:
                full_text += (page.extract_text() or "") + "\n"

            if not full_text.strip():
                return None

            numero_proposta, tipo_laudo = extrair_numero_proposta_e_tipo_laudo(full_text)
            modelo_usado = "eletronico" if "eletr" in tipo_laudo.lower() else "fisico"

            endereco, numero, complemento = extrair_endereco_numero_complemento(full_text)
            coords_str, lat, lon = extrair_coordenadas(full_text)

            tipo_imovel = _campo_texto(full_text, r'Tipo\s+do\s+Im[óo]vel')
            padrao_acabamento = _campo_texto(full_text, r'Padr[ãa]o\s+de\s+Acabamento\s+do\s+Im[óo]vel')
            estado_conservacao = _campo_texto(full_text, r'Estado\s+de\s+Conserva[çc][ãa]o\s+do\s+Im[óo]vel')

            area_averbada = _campo_numerico(full_text, r'[ÁA]rea\s+Averbada\s*\(em\s*m[²2]\)')
            area_comum = _campo_numerico(full_text, r'[ÁA]rea\s+Comum\s*\(em\s*m[²2]\)')
            area_total = _campo_numerico(full_text, r'[ÁA]rea\s+[Tt]otal\s*\(em\s*m[²2]\)')

            quartos = _campo_numerico(full_text, r'Total\s+de\s+Dormit[óo]rios', tipo='int')
            suites = _campo_numerico(full_text, r'N[°º]\s*de\s*Su[íi]tes', tipo='int')
            banheiros = _campo_numerico(full_text, r'Total\s+de\s+Banheiros', tipo='int')
            vagas = _campo_numerico(full_text, r'Total\s+de\s+Vagas', tipo='int')
            idade_anos = _campo_numerico(full_text, r'Idade\s+Aparente', tipo='int')

            data_avaliacao = extrair_data_vistoria(full_text)
            valor_avaliacao = extrair_valor_avaliacao(full_text)
            valor_unitario_m2 = extrair_valor_unitario_m2(full_text)

            return {
                "numero_proposta": numero_proposta,
                "codigo_laudo": extrair_codigo_laudo(full_text),
                "data_avaliacao": data_avaliacao,
                "endereco": endereco,
                "numero": numero,
                "complemento": complemento,
                "tipo_imovel": limpar_txt(tipo_imovel, "Apartamento"),
                "area_privativa_m2": converter_float_seguro(area_averbada),
                "area_comum_m2": converter_float_seguro(area_comum),
                "area_total_m2": converter_float_seguro(area_total),
                "quartos": converter_int_seguro(quartos),
                "suites": converter_int_seguro(suites),
                "banheiros": converter_int_seguro(banheiros),
                "vagas": converter_int_seguro(vagas),
                "idade_anos": converter_int_seguro(idade_anos),
                "padrao_acabamento": limpar_txt(padrao_acabamento, "Normal"),
                "estado_conservacao": limpar_txt(estado_conservacao, "Bom"),
                "valor_mercado": converter_float_seguro(valor_avaliacao),
                # o modelo do Itaú não tem um valor de "venda forçada"
                # separado (diferente do Santander) - fica zerado.
                "valor_venda_forcada": ZERO,
                "valor_unitario_m2": converter_float_seguro(valor_unitario_m2),
                "coordenadas": coords_str,
                "latitude": lat,
                "longitude": lon,
                "path": file_name,
                "modelo_usado": modelo_usado,
            }
    except Exception as e:
        print(f"[ERRO PARSER] {os.path.basename(pdf_path)}: {str(e)}")
        return None


def processar_em_lote():
    folder_path = r"data/laudos"
    if not os.path.exists(folder_path):
        folder_path = "."

    todos_pdfs = [f for f in os.listdir(folder_path) if f.endswith('.pdf')]
    print(f"Total de PDFs encontrados: {len(todos_pdfs)}")

    if not todos_pdfs:
        return

    host_pg = os.getenv("PGURL", "127.0.0.1")
    dbname = os.getenv("PGNAME", "testdb")
    user = os.getenv("PGUSR", "postgres")
    password = os.getenv("PGPASS", "postgres")
    port = os.getenv("PGPORT", "5432")

    try:
        conn = psycopg2.connect(host=host_pg, dbname=dbname, user=user, password=password, port=port)
    except Exception as e:
        print(f"[ERRO CARGA BANCO]: {str(e)}")
        return

    with conn:
        with conn.cursor() as cursor:
            # mesmas colunas da tabela 'laudos' (Santander) - só muda o
            # nome da tabela, pra manter os dois bancos com o mesmo
            # formato e permitir consultas/relatórios unificados depois.
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS laudos_itau (
                    id SERIAL PRIMARY KEY,
                    numero_proposta TEXT,
                    codigo_laudo TEXT,
                    data_avaliacao TEXT,
                    endereco TEXT,
                    numero TEXT,
                    complemento TEXT,
                    tipo_imovel TEXT,
                    area_privativa_m2 NUMERIC,
                    area_comum_m2 NUMERIC,
                    area_total_m2 NUMERIC,
                    quartos INTEGER,
                    suites INTEGER,
                    banheiros INTEGER,
                    vagas INTEGER,
                    idade_anos INTEGER,
                    padrao_acabamento TEXT,
                    estado_conservacao TEXT,
                    valor_mercado NUMERIC,
                    valor_venda_forcada NUMERIC,
                    valor_unitario_m2 NUMERIC,
                    coordenadas TEXT,
                    latitude DOUBLE PRECISION,
                    longitude DOUBLE PRECISION,
                    path TEXT,
                    modelo_usado TEXT
                );
            """)
            cursor.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS laudos_itau_codigo_laudo_key
                ON laudos_itau (codigo_laudo) WHERE codigo_laudo IS NOT NULL;
            """)

            # pula PDF que já está no banco - evita reextrair à toa
            cursor.execute("SELECT path FROM laudos_itau WHERE path IS NOT NULL;")
            ja_processados = {row[0] for row in cursor.fetchall()}

    pdf_files = [os.path.join(folder_path, f) for f in todos_pdfs if f not in ja_processados]
    pulados_ja_no_banco = len(todos_pdfs) - len(pdf_files)
    print(f"  {pulados_ja_no_banco} já estavam no banco (extração pulada), {len(pdf_files)} novo(s) pra processar.")

    if not pdf_files:
        print("Nada novo pra extrair.")
        conn.close()
        return

    num_workers = min(multiprocessing.cpu_count(), 8)
    dados_extraidos = []

    print(f"Iniciando extração paralela em {num_workers} workers...")
    with ProcessPoolExecutor(max_workers=num_workers) as executor:
        futures = {executor.submit(extrair_dados_pdf, f): f for f in pdf_files}
        for future in as_completed(futures):
            res_laudo = future.result()
            if res_laudo:
                dados_extraidos.append(res_laudo)

    print(f"Extração concluída. Total laudos: {len(dados_extraidos)}")
    if not dados_extraidos:
        conn.close()
        return

    colunas = list(dados_extraidos[0].keys())

    # Avisa se dois ou mais arquivos extraíram o MESMO código de laudo -
    # isso não deveria acontecer de verdade (cada inspeção tem um código
    # único), então é sinal de erro de extração nesses arquivos.
    arquivos_por_codigo = defaultdict(list)
    for d in dados_extraidos:
        if d["codigo_laudo"]:
            arquivos_por_codigo[d["codigo_laudo"]].append(d["path"])
    duplicados = {k: v for k, v in arquivos_por_codigo.items() if len(v) > 1}
    if duplicados:
        print("\n[AVISO] Mais de um arquivo extraiu o mesmo código de laudo (isso não deveria acontecer):")
        for codigo, arquivos in duplicados.items():
            print(f"  Código {codigo}: {', '.join(arquivos)}")
        print("  (o último desses arquivos processado vai prevalecer no banco -")
        print("  vale conferir manualmente a extração desses PDFs)\n")

    set_clause = ",\n            ".join(
        f"{col} = EXCLUDED.{col}" for col in colunas if col != "codigo_laudo"
    )
    placeholders = ", ".join(["%s"] * len(colunas))
    query_upsert_lote = f"""
        INSERT INTO laudos_itau ({', '.join(colunas)})
        VALUES %s
        ON CONFLICT (codigo_laudo) WHERE codigo_laudo IS NOT NULL DO UPDATE SET
            {set_clause};
    """
    query_upsert_linha = f"""
        INSERT INTO laudos_itau ({', '.join(colunas)})
        VALUES ({placeholders})
        ON CONFLICT (codigo_laudo) WHERE codigo_laudo IS NOT NULL DO UPDATE SET
            {set_clause};
    """

    por_codigo = {}
    sem_codigo = []
    for d in dados_extraidos:
        if d["codigo_laudo"]:
            por_codigo[d["codigo_laudo"]] = d
        else:
            sem_codigo.append(d)
    dados_para_gravar = list(por_codigo.values()) + sem_codigo

    try:
        with conn:
            with conn.cursor() as cursor:
                valores = [[dados[col] for col in colunas] for dados in dados_para_gravar]
                try:
                    execute_values(cursor, query_upsert_lote, valores, page_size=500)
                    print(f"[SUCESSO] {len(valores)} laudo(s) gravado(s) no banco (carga em lote). 0 com erro.")
                except Exception as e:
                    conn.rollback()
                    print(f"[AVISO] Carga em lote falhou ({str(e).splitlines()[0]}) - tentando linha por linha...")
                    gravados = 0
                    falhas = 0
                    for dados in dados_para_gravar:
                        linha_valores = [dados[col] for col in colunas]
                        try:
                            cursor.execute(query_upsert_linha, linha_valores)
                            gravados += 1
                        except Exception as e2:
                            conn.rollback()
                            falhas += 1
                            print(f"[ERRO - PULADO] {dados.get('path')}: {str(e2).splitlines()[0]}")
                    print(f"[SUCESSO] {gravados} laudo(s) gravado(s) no banco. {falhas} com erro (pulados).")
    except Exception as e:
        print(f"[ERRO CARGA BANCO]: {str(e)}")
    finally:
        conn.close()


if __name__ == "__main__":
    processar_em_lote()
