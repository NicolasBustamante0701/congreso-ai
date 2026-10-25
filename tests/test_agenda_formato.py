"""Partes de una sesión de la agenda — textos tal cual vinieron el 24/10/2026."""
from services.agenda_formato import partes


def test_sesion_con_invitado_y_tema():
    p = partes(
        "Sesión de la Comisión de Medio Ambiente Invitado: Señor José Angello Tangherlini Casal, "
        "ministro de Transportes y Comunicaciones. Tema: Superación de los ECA Ruido y Contaminación "
        "Acústica. (Ver otros invitados y temas en agenda)",
        "Comisión de Medio Ambiente y Sostenibilidad Presidente: Diputado Arturo Alegría García "
        "(Grupo Parlamentario Fuerza Popular)")
    assert p["titulo"] == "Comisión de Medio Ambiente y Sostenibilidad"
    assert p["actividad"] == ""
    assert p["invitado"] == "José Angello Tangherlini Casal — ministro de Transportes y Comunicaciones"
    assert p["tema"] == "Superación de los ECA Ruido y Contaminación Acústica"
    assert p["responsable"] == "Presidente: Diputado Arturo Alegría García (Fuerza Popular)"
    assert p["mas_temas"] is True


def test_invitado_con_ele_y_coletillas():
    p = partes(
        "Sesión de la Comisión de Asuntos de Salud lnvitado: Señor Alberto Ismael Beingolea Delgado , "
        "ministro de Cultura. Tema: Incidente ocurrido en el Complejo Arqueológico Chan Chan. "
        "-Otros temas en agenda. Agenda documentada",
        "Comisión en asuntos de Salud, Educación, Cultura Presidenta: Senadora Susana Matute Charún "
        "(Grupo Parlamentario Partido del Buen Gobierno)")
    assert p["invitado"] == "Alberto Ismael Beingolea Delgado — ministro de Cultura"
    assert p["tema"] == "Incidente ocurrido en el Complejo Arqueológico Chan Chan"
    assert "agenda" not in p["tema"].lower()
    assert p["responsable"].startswith("Presidenta: Senadora Susana Matute Charún")


def test_sesion_con_guion():
    p = partes(
        "Sesión de la Comisión de Constitución -Sustentación  de  la Proposición  Legislativa  "
        "00474-2026-2031-CD, que deroga la Ley 31810. -Otros temas en agenda. Agenda documentada",
        "Comisión de Constitución, Reglamento y Relaciones Exteriores Presidenta: Diputada Catherin "
        "Palomino Casavilca (Grupo Parlamentario Juntos Por el Perú)")
    assert p["titulo"] == "Comisión de Constitución, Reglamento y Relaciones Exteriores"
    assert p["tema"] == "Sustentación de la Proposición Legislativa 00474-2026-2031-CD, que deroga la Ley 31810"


def test_mesa_de_trabajo_conserva_la_actividad():
    p = partes("Mesa de trabajo “Impacto de las Plataformas Digitales en la economía peruana”",
               "Comisión de Trabajo y Seguridad Social. Presidente: Diputado Eduardo Castillo Rivas "
               "(Grupo Parlamentario Fuerza Popular)")
    assert p["titulo"] == "Comisión de Trabajo y Seguridad Social"
    assert p["actividad"] == "Mesa de trabajo “Impacto de las Plataformas Digitales en la economía peruana”"
    assert p["invitado"] == "" and p["tema"] == ""


def test_sin_organo_reconocible_no_pierde_nada():
    p = partes("Junta de Portavoces Primera Legislatura Ordinaria 2026 – 2027.",
               "Senador Miguel Torres Morales, presidente del Senado de la República")
    assert p["titulo"] == "Junta de Portavoces Primera Legislatura Ordinaria 2026 – 2027"
    assert p["responsable"] == "Senador Miguel Torres Morales, presidente del Senado de la República"
    assert p["organo"] == "" and p["mas_temas"] is False
    p = partes("Mesa técnica «Saneamiento integral de límites en el sector Oquendo»",
               "Diputado Carlos Yalta Sotelo Grupo Parlamentario Renovación Popular")
    assert p["titulo"] == "Mesa técnica «Saneamiento integral de límites en el sector Oquendo»"
    assert p["responsable"] == "Diputado Carlos Yalta Sotelo Grupo Parlamentario Renovación Popular"


def test_vacio():
    p = partes("", "")
    assert p["titulo"] == "" and p["responsable"] == ""


def test_grupo_de_trabajo_separa_la_comision():
    p = partes("Grupo de trabajo sobre grupos indígenas, originarios y afroperuano Sesión de instalación.",
               "Grupo de trabajo sobre grupos indígenas, originarios y afroperuano - Comisión en asuntos de "
               "Salud, Educación, Cultura Coordinadora: Senadora Susana Matute Churún (Grupo Parlamentario X)")
    assert p["titulo"] == "Grupo de trabajo sobre grupos indígenas, originarios y afroperuano"
    assert p["comision"] == "Comisión en asuntos de Salud, Educación, Cultura"
    assert p["actividad"] == "Sesión de instalación"
    p = partes("Mesa de trabajo Reunión.",
               "Grupo de trabajo sobre economías ilícitas, constituído en el ámbito de la Comisión de "
               "Economía, Banca, Finanzas e Inteligencia Financiera” Coordinador: Diputado Jaime Abensur Pinasco")
    assert p["titulo"] == "Grupo de trabajo sobre economías ilícitas"
    assert p["comision"] == "Comisión de Economía, Banca, Finanzas e Inteligencia Financiera"
