from pathlib import Path
import subprocess

from absl import app, flags, logging

FLAGS = flags.FLAGS

flags.DEFINE_integer("chunk", 5, "lecture chunk size")
flags.DEFINE_string("path", None, "out path")
flags.DEFINE_string("secret", None, "URL token")


def main(_):
    m = {
        # TODO: https://is.mff.cuni.cz/prednasky/prednaska/NMAF061/1
        # TODO: https://is.mff.cuni.cz/prednasky/prednaska/NMAF062/1
        # TODO: https://is.mff.cuni.cz/prednasky/prednaska/NMAG162/1
        # TODO: https://is.mff.cuni.cz/prednasky/prednaska/NOFY003/1
        # TODO: https://is.mff.cuni.cz/prednasky/prednaska/NOFY031/1
        # TODO: https://is.mff.cuni.cz/prednasky/prednaska/NTMF111/1
        ("NDMI011", 2016, "LS", "combinatorics-graphs-1"): 13,
        ("NDMI012", 2016, "ZS", "combinatorics-graphs-2"): 13,
        ("NMAF051", 2014, "ZS", "mathematical-analysis-1"): 23,
        ("NMAF052", 2014, "LS", "mathematical-analysis-2"): 27,
        ("NMAG101", 2015, "ZS", "linear-algebra-geometry-1"): 26,
        ("NMAG102", 2016, "LS", "linear-algebra-geometry-2"): 25,
        ("NMAG201", 2017, "ZS", "algebra-1"): 13,
        ("NMAG202", 2017, "LS", "algebra-2"): 13,
        ("NMAG301", 2019, "ZS", "commutative-rings"): 17,
        ("NMNM201", 2018, "ZS", "numeric-math"): 26,
        ("NOPT048", 2017, "LS", "optimalization-methods"): 13,
        ("NPFL122", 2019, "ZS", "deep-reinforcement-learning"): 10,
        ("NTIN060", 2014, "LS", "ads1"): 13,
        # ('NTIN071', 2018, 'LS', 'automata'): 13,
    }
    # TODO: kill all processes on our death
    for (code, year, semester, name), n in m.items():
        d = Path(FLAGS.path) / f"{code}-{name}"
        d.mkdir(exist_ok=True)
        url_pattern = (
            f"https://is.mff.cuni.cz/prednasky/play/{FLAGS.secret}/{code}_{year}_{semester}_[01-{n + 1:02d}].webm"
        )
        args = ["curl", url_pattern, "--output", "#1.webm", "--continue-at", "-"]
        logging.info("running: %s", " ".join(args))
        subprocess.Popen(args, cwd=d).wait()


if __name__ == "__main__":
    flags.mark_flag_as_required("path")
    app.run(main)
