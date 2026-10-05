from __future__ import annotations

from rich.text import Text

DOBERMAN = """                 =
                :#=
                *##.
               -###*
               *####
               #####-
              -#####*
              *######-::..
             -#########%%%##+-::..
            :###########+++############*+==-::.
           .##########+:.@+#####################+
          .#####################################+
          #####################-:.VV  Vv  VV Vv
         *#################+%%%#=   ^   A vV
        -##################%%%%%+#+=A==AA^---:
        ###################%%%%%%%%%%%+#######.
       -####################%%%%%%%%%+=--:..
       #################=
      :##################
     ====o===o===o===o======
      +###################-
      #####################.
"""

MASK = """hhhhhhhhhhhhhhhhhh
hhhhhhhhhhhhhhhhhhh
hhhhhhhhhhhhhhhhhhhh
hhhhhhhhhhhhhhhhhhhh
hhhhhhhhhhhhhhhhhhhh
hhhhhhhhhhhhhhhhhhhhh
hhhhhhhhhhhhhhhhhhhhh
hhhhhhhhhhhhhhhhhhhhhhhhhh
hhhhhhhhhhhhhhhhhhhhhhhttthhhhhhhh
hhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhh
hhhhhhhhhhhhhhhhhhhhhhhhhehhhhhhhhhhhhhhhhhhhhhhh
hhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhh
hhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhffhhffhhffhff
hhhhhhhhhhhhhhhhhhhhhhhhhhhtttthhhhhfhhhfhff
hhhhhhhhhhhhhhhhhhhhhhhhhhhtttttthhhfhhfffhhhh
hhhhhhhhhhhhhhhhhhhhhhhhhhhtttttttttttthhhhhhhh
hhhhhhhhhhhhhhhhhhhhhhhhhhhhttttttttthhhhhhh
hhhhhhhhhhhhhhhhhhhhhhhhh
hhhhhhhhhhhhhhhhhhhhhhhhh
hhhhhccccecccecccecccecccccc
hhhhhhhhhhhhhhhhhhhhhhhhhhh
hhhhhhhhhhhhhhhhhhhhhhhhhhhh
"""

TITLE = "K W A T C H D O G"
TAGLINE = "Who’s a good daemon?"

STYLES = {
    "h": "#ff1a1a",
    "t": "#8b0000",
    "f": "bold #ff1a1a",
    "c": "#8b0000",
    "e": "bold #ff1a1a",
}


def doberman_text() -> Text:
    text = Text(no_wrap=True)
    for line, mask in zip(DOBERMAN.splitlines(), MASK.splitlines()):
        for ch, m in zip(line, mask):
            text.append(ch, STYLES.get(m, "#ff1a1a") if ch != " " else None)
        text.append("\n")
    text.rstrip()
    return text
