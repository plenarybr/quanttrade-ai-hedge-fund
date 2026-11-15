# Claude pela sua conta + Discord

Duas mudanças deste fork em relação ao upstream:

1. **Os agentes raciocinam no Claude da sua conta**, pelo CLI `claude` instalado na
   máquina — assinatura, não API key.
2. **O fundo fala no Discord**: cada sessão do paper fund, cada halt/gap/falha e
   cada backtest terminado viram mensagem no canal. E o canal responde:
   `/status`, `/scorecard`, `/halt`, `/resume`, `/run`, `/stop`.

Nada disso é obrigatório. Sem webhook configurado, o fundo roda exatamente como
antes; com `--model claude-opus-5-5` (ou qualquer id do registry), você volta a
pagar por token na API.

---

## 1. Claude pela conta

### Como funciona

O provider novo (`hedge_fund/llm/claude_code.py`) é um `LLMClient` como qualquer
outro: um processo `claude -p` por chamada de agente, prompt na entrada padrão,
JSON na saída. Sem ferramentas, sem MCP, sem sessão em disco — é uma completion,
não uma conversa.

Três decisões que valem conhecer:

- **`ANTHROPIC_API_KEY` é removida do ambiente do processo filho.** Com ela
  setada, o CLI prefere a key e cobra por token — o oposto do motivo deste
  provider existir. Para forçar o contrário: `HEDGE_FUND_CLAUDE_ALLOW_API_KEY=1`.
- **O prompt vai por stdin**, nunca em `argv`: prompt de fundamentals aparece
  inteiro no `ps` e estoura `ARG_MAX`.
- **As flags de isolamento (`--tools ""`, `--strict-mcp-config`, …) são
  opcionais.** Se o CLI instalado for velho e recusar uma delas, a chamada é
  repetida uma vez sem elas e isso fica memorizado no processo.

### No seu Mac

```bash
claude login
```

É só isso. O modelo padrão do fork já é `claude-code:opus`:

```bash
aihf backtest ~/.hedge-fund/mandates/example.yaml --universe AAPL,MSFT
```

Para trocar: `--model claude-code:sonnet`, ou `HEDGE_FUND_LLM_MODEL=claude-code:sonnet`
no `.env`. Na TUI, `m` abre o seletor — as três primeiras linhas são as da conta
e não pedem key nenhuma.

### No servidor (headless)

`claude login` abre navegador, o que não existe num servidor. O caminho é gerar
um token de longa duração **na sua máquina** e exportar lá:

```bash
claude setup-token
```

Copie o token (`sk-ant-oat…`) para `/etc/aihf/aihf.env` no servidor:

```
CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat...
```

Esse token é uma credencial da sua conta: 0600, dono certo, e nunca commitado.

### Variáveis

| variável | para quê |
| --- | --- |
| `CLAUDE_CODE_OAUTH_TOKEN` | login headless (servidor) |
| `HEDGE_FUND_CLAUDE_BIN` | caminho do `claude`, se não estiver no PATH |
| `HEDGE_FUND_CLAUDE_ARGS` | flags extras em toda chamada (proxy, `--settings`, …) |
| `HEDGE_FUND_CLAUDE_ALLOW_API_KEY` | `1` deixa a API key ganhar de novo |
| `HEDGE_FUND_LLM_MODEL` | modelo padrão (`claude-code:opus` por default) |

### Quando dá errado

| o que aparece | o que é |
| --- | --- |
| `OAuth session expired … run claude login` | sessão do CLI venceu: `claude login` de novo, ou token novo |
| `no 'claude' CLI on this machine` | CLI não instalado ou fora do PATH → `HEDGE_FUND_CLAUDE_BIN` |
| `usage limit … the account's limit` | limite da assinatura, não de key; passa sozinho |
| `the Claude CLI did not answer within …` | timeout (padrão 300s por chamada) |

Teste rápido, sem gastar um backtest inteiro:

```bash
python -c "from hedge_fund.llm import check_login; print(check_login() or 'ok')"
```

---

## 2. Discord — mandando mensagem

### Webhook (é só isso)

No Discord: **Canal → Editar canal → Integrações → Webhooks → Novo webhook →
Copiar URL**. Em `~/.hedge-fund/.env`:

```
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/.../...
```

Confere antes de precisar:

```bash
aihf discord test
```

Só URLs em `discord.com` (e canary/ptb) são aceitas — um `.env` com host errado
falha na hora de configurar, não depois de publicar o book num host qualquer.

### O que é postado

| quando | o quê |
| --- | --- |
| cada sessão avançada (`paper tick`, `paper run`) | NAV e retorno, caixa, ordens executadas e custo, book, decisão pendente, clamps de risco |
| stop-out por drawdown | a mesma mensagem, em vermelho, no canal de alertas |
| gap, halt, resume, falha do runner | o evento com o motivo que o engine registrou |
| backtest terminado | retorno vs benchmark, excesso, sharpe, drawdown, ordens, custos |

Opcionais:

```
DISCORD_ALERT_WEBHOOK_URL=...     # só o que precisa de olho, em outro canal
DISCORD_NOTIFY=sessions,alerts,backtests   # default: tudo
```

E `aihf --no-discord <comando>` silencia uma execução específica.

Uma notificação **nunca** derruba o fundo: Discord fora do ar, rate limit ou
mensagem recusada viram linha de log e nada mais.

---

## 3. Discord — o bot que responde

Precisa de `pip install 'aihf[discord]'` e de um processo próprio. O webhook
acima não precisa de nada disso.

1. <https://discord.com/developers/applications> → **New Application**.
2. **Bot** → **Reset Token** → copie para `DISCORD_BOT_TOKEN`.
   Nenhum *privileged intent* é necessário — o bot não lê mensagens.
3. **OAuth2 → URL Generator**: escopos `bot` e `applications.commands`;
   permissão `Send Messages`. Abra a URL e convide para o servidor.
4. Pegue o id do servidor (Discord → Configurações → Avançado → Modo
   desenvolvedor; clique direito no servidor → Copiar ID) e o seu id de usuário.

```
DISCORD_BOT_TOKEN=...
DISCORD_GUILD_ID=123...            # comandos aparecem na hora (sem isso, até 1h)
DISCORD_ALLOWED_USER_IDS=456...    # quem pode halt/resume/run/stop
DISCORD_ALLOWED_CHANNEL_IDS=789... # opcional: onde os comandos funcionam
```

```bash
aihf discord bot
```

| comando | o que faz | precisa de allowlist |
| --- | --- | --- |
| `/funds` | todos os paper funds, NAV e estado | não |
| `/status <fund>` | NAV, última sessão, decisão pendente, book | não |
| `/scorecard <fund>` | analistas por IC, estratégias por P&L | não |
| `/halt <fund> <motivo>` | liga o kill switch | **sim** |
| `/resume <fund>` | desliga | **sim** |
| `/run <fund>` | sobe o runner do fundo | **sim** |
| `/stop <fund>` | para depois da sessão em voo | **sim** |

**Com `DISCORD_ALLOWED_USER_IDS` vazia, ninguém escreve.** Um bot convidado para
um servidor sem configuração responde perguntas; não para um fundo. Leituras
continuam liberadas (restrinja por canal se quiser).

---

## 4. Servidor

Arquivos prontos em [`deploy/`](../deploy): duas units do systemd e um
`docker-compose.yml`. Os dois caminhos assumem que o Claude CLI está instalado e
que `CLAUDE_CODE_OAUTH_TOKEN` está no arquivo de ambiente.

### Passo a passo (systemd)

```bash
# 1. usuário e diretórios
sudo useradd -r -m -d /home/aihf -s /bin/bash aihf
sudo mkdir -p /opt/aihf /etc/aihf

# 2. o código
sudo -u aihf git clone https://github.com/plenarybr/quanttrade-ai-hedge-fund /opt/aihf
cd /opt/aihf
sudo -u aihf python3 -m venv .venv
sudo -u aihf .venv/bin/pip install -e '.[discord]'

# 3. o Claude CLI (instalador oficial; alternativa: npm i -g @anthropic-ai/claude-code)
sudo -u aihf bash -lc 'curl -fsSL https://claude.ai/install.sh | bash'
sudo -u aihf /home/aihf/.local/bin/claude --version

# 4. o ambiente (token do `claude setup-token`, webhook, keys de dados)
sudo install -m 600 -o aihf -g aihf /dev/null /etc/aihf/aihf.env
sudo -e /etc/aihf/aihf.env

# 5. as units
sudo cp deploy/aihf-paper@.service deploy/aihf-discord-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now aihf-paper@alpha      # um fundo chamado "alpha"
sudo systemctl enable --now aihf-discord-bot
```

Conferindo:

```bash
systemctl status aihf-paper@alpha
journalctl -u aihf-paper@alpha -f
sudo -u aihf /opt/aihf/.venv/bin/aihf discord test
```

O `/etc/aihf/aihf.env` é lido pelo systemd (`EnvironmentFile`), então use
`CHAVE=valor` por linha, sem `export`, e aspas em valores com espaço.

### Docker

```bash
cd deploy
cp ../.env.example aihf.env   # e edite
docker compose up -d
docker compose logs -f runner
```

O compose monta `./hedge-fund-data` como `~/.hedge-fund` do contêiner: é onde
ficam os ledgers. Faça backup disso — o NAV de um paper fund é o track record,
e ele não se reconstrói.

### Checklist antes de deixar rodando

- [ ] `aihf discord test` postou no canal certo
- [ ] `check_login()` respondeu `ok` com o token do servidor
- [ ] `aihf paper status <fund>` mostra o fundo que você espera
- [ ] relógio do servidor em UTC e NTP ligado (o engine usa New York por conta própria)
- [ ] `/etc/aihf/aihf.env` em 0600 e fora do git
- [ ] backup de `~/.hedge-fund/paper/`

---

## 5. Custos e limites

Rodando na assinatura, cada agente LLM por ticker por sessão é uma chamada ao
CLI. O cache de prompt do projeto (`~/.hedge-fund/cache/llm/`) continua valendo:
snapshot igual não paga chamada nova. Mesmo assim, um fundo de minuto com muitos
nomes e vários analistas consome limite rápido — comece diário, meça, e só então
aumente a cadência.

O cache separa conta de API: a mesma pergunta respondida por `claude-code:opus`
e por `claude-opus-5-5` são dois registros, porque não são a mesma coisa para
quem paga.
