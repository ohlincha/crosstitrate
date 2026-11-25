"""
Known instruments of the Metrohm Titrino family (RS232 remote control).

All share the command language in protocol.py; the instrument reports its
program version via &Config.Aux.Prog, e.g. "718.0010", whose prefix is the
model number. Unknown models fall back to a generic profile.

`verified` marks models tested with real hardware using this software.
"""


class Profile:
    def __init__(self, model, name, potentiometric=True, multi_burette=False, meas_mode="MEAS",
                 quantities=("U", "pH"), verified=False, notes=""):
        self.model = model
        self.name = name
        self.potentiometric = potentiometric   # can measure U (mV) / pH
        self.multi_burette = multi_burette     # supports &Assembly.Bur.Select (D0/D1/D2)
        self.meas_mode = meas_mode
        self.quantities = quantities
        self.verified = verified
        self.notes = notes

    @property
    def title(self):
        return f"{self.model} {self.name}"


PROFILES = {p.model: p for p in [
    Profile("702", "SM Titrino",
            notes="Used in the lab with WinTitrate, which sends the same commands in short form (&M, &A.B.M.V, "
                  "&I.A.T.M ...) but never the detailed status $D that CrossTitrate relies on: not yet tried "
                  "with CrossTitrate."),
    Profile("716", "DMS Titrino"),
    Profile("718", "STAT Titrino", notes="Reference model: command set taken from its manual."),
    Profile("719", "S Titrino"),
    Profile("720", "KFS Titrino", potentiometric=False, quantities=("Ipol", "Upol"),
            notes="Karl Fischer titrator: no mV/pH measurement; usable as a burette."),
    Profile("736", "GP Titrino"),
    Profile("751", "GPD Titrino", multi_burette=True),
    Profile("758", "KFD Titrino", potentiometric=False, quantities=("Ipol", "Upol"),
            notes="Karl Fischer titrator: usable as a burette."),
    Profile("784", "KFP Titrino", potentiometric=False, quantities=("Ipol", "Upol"),
            notes="Karl Fischer titrator: usable as a burette."),
    Profile("785", "DMP Titrino", multi_burette=True, verified=True,
            notes="Tested with a real 785 DMP (program 785.0011): identify, EMF, dosing, filling."),
    Profile("794", "Basic Titrino"),
    Profile("795", "KFT Titrino", potentiometric=False, quantities=("Ipol", "Upol"),
            notes="Karl Fischer titrator: usable as a burette."),
    Profile("798", "MPT Titrino", multi_burette=True),
    Profile("799", "GPT Titrino", multi_burette=True),
]}

GENERIC = Profile("?", "Titrino (unknown model)",
                  notes="Model not recognised; assuming the standard Titrino command set.")


def profile_for_prog(prog):
    """Profile for a program version string such as '718.0010'."""
    prefix = (prog or "").strip().split(".")[0][-3:]
    return PROFILES.get(prefix, GENERIC)
