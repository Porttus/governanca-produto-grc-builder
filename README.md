# Governança de Produto — GRC Builder

Dashboard executivo de acompanhamento do produto GRC Builder (Porttus), com dados extraídos diretamente do Azure DevOps.

**Link publicado (GitHub Pages):** https://porttus.github.io/governanca-produto-grc-builder/

## Conteúdo

- **Governança de Produto** — árvore de Épicos e Discovery, com progresso real
- **Timeline** — cronograma de entregas (Feature/Solicitação/Discovery) com datas reais e Roadmap Visual
- **Flow Metrics** — Lead Time, Cycle Time, Throughput, WIP, Bugs, Produtividade por desenvolvedor, tudo filtrável por semana/mês/ano
- **Visão Estratégica** — roadmap por nível de impacto (Evolução Relevante / Transformação / Inovação Disruptiva), com as demandas reais de Discovery do Azure DevOps

## Atualização automática

Um workflow do GitHub Actions (`.github/workflows/refresh.yml`) roda **todos os dias às 08h (horário de Brasília)**, busca dados frescos no Azure DevOps via `scripts/refresh_data.py`, e republica o `index.html` automaticamente — sem intervenção manual.

Esse script atualiza:
- Árvore de Épicos e Discovery (progresso, estados)
- Cards da Timeline (datas, progresso)
- Todas as métricas de Flow Metrics (Lead/Cycle Time, Throughput, WIP, Bugs, Story Points, etc.)

**O que NÃO é atualizado automaticamente:** a aba "Visão Estratégica" (o roadmap embutido com classificação de impacto) precisa de atualização manual, já que envolve decisões editoriais (categoria, prioridade, impacto) que não vêm diretamente do Azure DevOps.

### Rodar manualmente

Pela aba **Actions** deste repositório → workflow "Atualizar dados do dashboard" → **Run workflow**.

### Manutenção do token

O secret `AZURE_DEVOPS_PAT` (usado pela automação) tem validade de 90 dias. Quando expirar, gere um novo Personal Access Token no Azure DevOps (permissão de leitura em Work Items) e atualize o secret em **Settings → Secrets and variables → Actions → AZURE_DEVOPS_PAT**.
