import requests
import argparse
import os
from llama_cpp import Llama, LlamaGrammar
import numpy as np
from urllib.request import urlretrieve
import drain3
from drain3.template_miner_config import TemplateMinerConfig
import logging
import sys
import progressbar

DEFAULT_ADVISOR = "https://huggingface.co/TheBloke/Mistral-7B-Instruct-v0.2-GGUF/resolve/main/mistral-7b-instruct-v0.2.Q4_K_S.gguf?download=true"

DEFAULT_LLM_RATER = "https://huggingface.co/TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF/resolve/main/tinyllama-1.1b-chat-v1.0.Q4_K_S.gguf?download=true"

PROMPT_TEMPLATE = """
Given following log snippets, and nothing else, explain what failure, if any occured during build of this package.
Ignore strings wrapped in <: :>, such as <:*:>.

{}

Analysis of the failure must be in a format of [X] : [Y], where [X] is a log snippet, and [Y] is the explanation.

Finally, drawing on information from all snippets, provide complete explanation of the issue.

Analysis:

"""

SUMMARIZE_PROPT_TEMPLATE = """
Does following log contain error or issue?

Log:

{}

Answer:

"""

CACHE_LOC = "~/.cache/logdetective/"

LOG = logging.getLogger("logdetective")


class MyProgressBar():
    def __init__(self):
        self.pbar = None

    def __call__(self, block_num, block_size, total_size):
        if not self.pbar:
            self.pbar=progressbar.ProgressBar(maxval=total_size)
            self.pbar.start()

        downloaded = block_num * block_size
        if downloaded < total_size:
            self.pbar.update(downloaded)
        else:
            self.pbar.finish()


def get_chunks(text: str):
    """Split log into chunks according to heuristic
    based on whitespace and backslash presence.
    """
    text_len = len(text)
    i = 0
    chunk = ""
    while i < text_len:
        chunk += text[i]
        if text[i] == '\n':
            if i+1 < text_len and (text[i+1].isspace() or text[i-1] == "\\"):
                i += 1
                continue
            yield chunk
            chunk = ""
        i += 1


class LLMExtractor:
    """
    A class that extracts relevant information from logs using a language model.
    """
    def __init__(self, model_path: str, verbose: bool):
        self.model = Llama(
            model_path=model_path,
            n_ctx=0,
            verbose=verbose)
        self.grammar = LlamaGrammar.from_string(
            "root ::= (\"Yes\" | \"No\")", verbose=False)

    def __call__(self, log: str, n_lines: int = 2, neighbors: bool = False) -> str:
        chunks = self.rate_chunks(log, n_lines)
        out = self.create_extract(chunks, neighbors)
        return out

    def rate_chunks(self, log: str, n_lines: int = 2) -> list[tuple]:

        results = []
        log_lines = log.split("\n")

        for i in range(0, len(log_lines), n_lines):
            block = '\n'.join(log_lines[i:i+n_lines])
            prompt = SUMMARIZE_PROPT_TEMPLATE.format(log)
            out = self.model(prompt, max_tokens=7, grammar=self.grammar)
            out = f"{out['choices'][0]['text']}\n"
            results.append((block, out))

        return results


    def create_extract(self, chunks: list[tuple], neighbors: bool = False) -> str:

        interesting = []
        summary = ""
        for i in range(len(chunks)):
            if chunks[i][1].startswith("Yes"):
                interesting.append(i)
                if neighbors:
                    interesting.extend([max(i-1, 0), min(i+1, len(chunks)-1)])

        interesting = np.unique(interesting)

        for i in interesting:
            summary += chunks[i][0] + "\n"

        return summary


class DrainExtractor:
    """A class that extracts information from logs using a template miner algorithm.
    """
    def __init__(self, verbose: bool = False, context: bool = False):
        config = TemplateMinerConfig()
        config.load(f"{os.path.dirname(__file__)}/drain3.ini")
        config.profiling_enabled = verbose
        self.miner = drain3.TemplateMiner(config=config)
        self.verbose = verbose
        self.context = context

    def __call__(self, log: str) -> str:
        out = ""
        for chunk in get_chunks(log):
            procesed_line = self.miner.add_log_message(chunk)
            LOG.debug(procesed_line)
        sorted_clusters = sorted(self.miner.drain.clusters, key=lambda it: it.size, reverse=True)
        for chunk in get_chunks(log):
            cluster = self.miner.match(chunk, "always")
            if cluster in sorted_clusters:
                out += f"{chunk}\n"
                sorted_clusters.remove(cluster)
        return out


def download_model(url: str, verbose: bool = False) -> str:
    """ Downloads a language model from a given URL and saves it to the cache directory.

    Args:
        url (str): The URL of the language model to be downloaded.

    Returns:
        str: The local file path of the downloaded language model.
    """
    path = os.path.join(
        os.path.expanduser(CACHE_LOC), url.split('/')[-1])

    LOG.info(f"Downloading model from {url} to {path}")
    if not os.path.exists(path):
        if verbose:
            path, status = urlretrieve(url, path, MyProgressBar())
        else:
            path, status = urlretrieve(url, path)

    return path


def process_log(log: str, model: Llama) -> str:
    """
    Processes a given log using the provided language model and returns its summary.

    Args:
        log (str): The input log to be processed.
        model (Llama): The language model used for processing the log.

    Returns:
        str: The summary of the given log generated by the language model.
    """
    return model(PROMPT_TEMPLATE.format(log), max_tokens=0)["choices"][0]["text"]


def main():
    parser = argparse.ArgumentParser("logdetective")
    parser.add_argument("url", type=str, default="")
    parser.add_argument("-M", "--model", type=str, default=DEFAULT_ADVISOR)
    parser.add_argument("-S", "--summarizer", type=str, default="drain")
    parser.add_argument("-N","--n_lines", type=int, default=5)
    parser.add_argument("-v", "--verbose", action='count', default=0)
    parser.add_argument("-q", "--quiet", action='store_true')

    args = parser.parse_args()


    if args.verbose and args.quiet:
        sys.stderr.write("Error: --quiet and --verbose is mutually exclusive.\n")
        sys.exit(2)
    log_level = logging.INFO
    if args.verbose >= 1:
        log_level = logging.DEBUG
    if args.quiet:
        log_level = 0
    logging.basicConfig(stream=sys.stdout)
    LOG.setLevel(log_level)

    if not os.path.exists(CACHE_LOC):
        os.makedirs(os.path.expanduser(CACHE_LOC), exist_ok=True)

    if not os.path.isfile(args.model):
        model_pth = download_model(args.model, not args.quiet)
    else:
        model_pth = args.model

    if args.summarizer == "drain":
        extractor = DrainExtractor(args.verbose > 1, context=True)
    elif os.path.isfile(args.summarizer):
        extractor = LLMExtractor(args.summarizer, args.verbose > 1)
    else:
        summarizer_pth = download_model(args.summarizer, not args.quiet)
        extractor = LLMExtractor(summarizer_pth)

    LOG.info("Getting summary")
    model = Llama(
        model_path=model_pth,
        n_ctx=0,
        verbose=args.verbose > 2)

    log = requests.get(args.url).text
    log_summary = extractor(log)

    ratio = len(log_summary.split('\n'))/len(log.split('\n'))
    LOG.debug(f"Log summary: \n{log_summary}")
    LOG.info(f"Compression ratio: {ratio}")

    LOG.info("Analyzing the text")
    print(f"Explanation: \n{process_log(log_summary, model)}")


if __name__ == "__main__":
    main()
