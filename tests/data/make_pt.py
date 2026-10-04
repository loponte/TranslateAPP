"""Fixtures PT-BR (edge-tts), irmãs dos fixtures EN: reaproveita build/save/noisy de make_fixtures.py.
Saída: tests/data/conv_pt_2spk.wav, conv_pt_3spk.wav, conv_pt_2spk_noisy.wav (+ .json). Cache próprio em _cache_pt/
(o make_fixtures.py poda o _cache/ dele). Da raiz do projeto:
    uv run --no-project --with edge-tts --with numpy python tests/data/make_pt.py
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import make_fixtures as mf  # noqa: E402

mf.CACHE = HERE / "_cache_pt"
OUT = HERE

V2 = {"A": "pt-BR-AntonioNeural", "B": "pt-BR-FranciscaNeural"}
S2 = [
    ("A", "E aí Camila, tá me ouvindo? | O meu microfone tá funcionando?"),
    ("B", "Tô sim, tá tudo certo. Desculpa o atraso, o meu computador travou de novo."),
    ("A", "Sem problema. | Então, sobre o projeto novo, | a gente precisa fechar o plano até sexta."),
    ("B", "Beleza, entendi."),
    ("A", "Faltam umas coisas ainda. | Primeiro, o servidor de partidas continua caindo quando entra muita gente. "
          "| Depois, tem um bug chato na tela de inventário. | E o Marcos tá de férias até segunda."),
    ("B", "Peraí, sério?"),
    ("A", "Pois é, o voo dele mudou. A correção do inventário ainda tá esperando revisão."),
    ("B", "Tá bom, eu reviso logo depois dessa chamada. Mas eu tô mais preocupada com o lag nos servidores da Europa."),
    ("A", "É verdade."),
    ("B", "Os jogadores do Brasil estão reclamando muito do ping, | não dá pra jogar desse jeito."),
    ("A", "Que chato. Será que dá pra subir um servidor aqui em São Paulo?"),
    ("B", "Vai sair um pouco caro, mas eu acho que vale a pena. | Dá pra testar no ambiente de homologação antes."),
    ("A", "Perfeito. E o chefão novo? A galera tá dizendo que o ataque de fogo dele é muito forte."),
    ("B", "Verdade, eu vou diminuir o dano | e aumentar o tempo de recarga da habilidade."),
    ("A", "Boa. | Então, | a atualização vai pro ar na sexta à noite. "
          "E a gente ainda precisa escrever as notas da versão."),
    ("B", "Calma aí, sexta à noite? Isso é de madrugada pro pessoal da Alemanha."),
    ("A", "Ah, é mesmo."),
    ("B", "Vamos fazer na quinta na hora do almoço, e deixa a sexta como plano reserva."),
    ("A", "Fechado. Eu atualizo a agenda e aviso todo mundo no canal."),
    ("B", "Show. | Falamos depois?"),
    ("A", "Falou, até amanhã. | E não esquece de mandar o teu código antes de desligar."),
]
G2 = mf.G2

V3 = {"A": "pt-BR-AntonioNeural", "B": "pt-BR-FranciscaNeural", "C": "pt-BR-ThalitaMultilingualNeural"}
S3 = [
    ("A", "Bom dia, pessoal. Vamos começar? Obrigado por entrarem."),
    ("B", "Bom dia."),
    ("C", "Oi, gente."),
    ("A", "Primeiro assunto, o lançamento do aplicativo. Juliana, você pode dar uma atualização rápida?"),
    ("C", "Claro. A gente fechou quase todas as tarefas da semana, e os travamentos caíram bastante depois que "
          "corrigimos o vazamento de memória no carregador de imagens. O único problema que sobrou é a tela de "
          "pagamento nos celulares Android mais antigos, e o João acha que consegue resolver até quarta-feira."),
    ("A", "Ótimo. Quarta é realista?"),
    ("C", "Acho que sim."),
    ("B", "Parece bom."),
    ("A", "Certo. Fernanda, como está a parte do servidor?"),
    ("B", "A nova busca finalmente está em homologação, e o tempo de resposta caiu muito. A gente ainda precisa "
          "fazer um teste de carga com milhares de usuários antes de publicar em produção, e eu queria fazer isso "
          "na quinta à noite, quando o tráfego é menor, pra ninguém ser acordado de madrugada."),
    ("C", "Faz sentido."),
    ("A", "Tem risco de a gente perder o prazo de outubro?"),
    ("B", "Só se o teste falhar."),
    ("A", "Justo. Mais alguma coisa?"),
    ("C", "Por mim é isso."),
    ("B", "Por mim também."),
    ("A", "Valeu, pessoal. Até a semana que vem."),
]
G3 = mf.G4

for name, v, s, g in (("conv_pt_2spk", V2, S2, G2), ("conv_pt_3spk", V3, S3, G3)):
    y, gab = mf.build(v, s, g)
    mf.save(OUT / f"{name}.wav", y)
    (OUT / f"{name}.json").write_text(json.dumps(gab, ensure_ascii=False, indent=1), encoding="utf-8")
    print(name, f"{len(y) / mf.SR:.1f} s", len(gab["turns"]), "turnos")
    if name == "conv_pt_2spk":
        mf.save(OUT / "conv_pt_2spk_noisy.wav", mf.noisy(y, gab["turns"]))
        (OUT / "conv_pt_2spk_noisy.json").write_text(json.dumps(gab, ensure_ascii=False, indent=1), encoding="utf-8")
