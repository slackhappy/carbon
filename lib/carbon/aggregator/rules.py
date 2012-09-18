import time
import re
from os.path import exists, getmtime
from twisted.internet.task import LoopingCall
from carbon import log
from carbon.conf import settings
from carbon.aggregator.buffers import BufferManager

if settings.AGGREGATION_RULES_CACHE_SIZE != float('inf'):
  try:
    import ordereddict
    USE_LRU_RULES_CACHE = True
  except ImportError:
    log.err("Failed to import ordereddict (needed to limit AGGREGATION_RULES_CACHE_SIZE)")
    USE_LRU_RULES_CACHE = False
else:
  USE_LRU_RULES_CACHE = False



class RuleManager:
  def __init__(self):
    self.rules_file = None
    self.read_task = LoopingCall(self.read_rules)
    self.rules_last_read = 0.0
    self.clear()

  def clear(self):
    if USE_LRU_RULES_CACHE:
      self.cache = ordereddict.OrderedDict()
      self.cache_max_size = int(settings.AGGREGATION_RULES_CACHE_SIZE)
    else:
      self.cache = {}
    self.rules = []

  def read_from(self, rules_file):
    self.rules_file = rules_file
    self.read_rules()
    self.read_task.start(10, now=False)

  def read_rules(self):
    if not exists(self.rules_file):
      self.clear()
      return

    # Only read if the rules file has been modified
    try:
      mtime = getmtime(self.rules_file)
    except:
      log.err("Failed to get mtime of %s" % self.rules_file)
      return
    if mtime <= self.rules_last_read:
      return

    # Read new rules
    log.aggregator("reading new aggregation rules from %s" % self.rules_file)
    new_rules = []
    for line in open(self.rules_file):
      line = line.strip()
      if line.startswith('#') or not line:
        continue

      rule = self.parse_definition(line)
      new_rules.append(rule)

    log.aggregator("clearing aggregation buffers")
    BufferManager.clear()
    self.rules = new_rules
    self.rules_last_read = mtime

  def parse_definition(self, line):
    try:
      left_side, right_side = line.split('=', 1)
      output_pattern, frequency = left_side.split()
      method, input_pattern = right_side.split()
      frequency = int( frequency.lstrip('(').rstrip(')') )
      return AggregationRule(input_pattern, output_pattern, method, frequency)

    except:
      log.err("Failed to parse line: %s" % line)
      raise

  # return a list of (rule, result) tuples for a metric
  def get_aggregate_metrics(self, metric_path):
    if not self.rules:
      return ()
    try:
      if USE_LRU_RULES_CACHE:
        results = self.cache.pop(metric_path)
        self.cache[metric_path] = results
        return results
      else:
        return self.cache[metric_path]
    except KeyError:
      results = []
      for rule in self.rules:
        result = rule.get_aggregate_metric(metric_path)
        if result != None:
          results.append((rule, result))
      if results:
        self.cache[metric_path] = results
      else:
        self.cache[metric_path] = ()  # use immutable tuple singleton
      if USE_LRU_RULES_CACHE:
        while len(self.cache) > self.cache_max_size:
          try:
            self.cache.popitem(False)
          except KeyError:
            pass

      return results 

class AggregationRule:
  def __init__(self, input_pattern, output_pattern, method, frequency):
    self.input_pattern = input_pattern
    self.output_pattern = output_pattern
    self.method = method
    self.frequency = int(frequency)

    if method not in AGGREGATION_METHODS:
      raise ValueError("Invalid aggregation method '%s'" % method)

    self.aggregation_func = AGGREGATION_METHODS[method]
    self.build_regex()
    self.build_template()

  def get_aggregate_metric(self, metric_path):
    match = self.regex.match(metric_path)
    result = None

    if match:
      extracted_fields = match.groupdict()
      try:
        result = self.output_template % extracted_fields
      except:
        log.err("Failed to interpolate template %s with fields %s" % (self.output_template, extracted_fields))

    return result

  def build_regex(self):
    input_pattern_parts = self.input_pattern.split('.')
    regex_pattern_parts = []

    for input_part in input_pattern_parts:
      if '<<' in input_part and '>>' in input_part:
        i = input_part.find('<<')
        j = input_part.find('>>')
        pre = input_part[:i]
        post = input_part[j+2:]
        field_name = input_part[i+2:j]
        regex_part = '%s(?P<%s>.+)%s' % (pre, field_name, post)

      else:
        i = input_part.find('<')
        j = input_part.find('>')
        if i > -1 and j > i:
          pre = input_part[:i]
          post = input_part[j+1:]
          field_name = input_part[i+1:j]
          regex_part = '%s(?P<%s>[^.]+)%s' % (pre, field_name, post)
        elif input_part == '*':
          regex_part = '[^.]+'
        else:
          regex_part = input_part.replace('*', '[^.]*')

      regex_pattern_parts.append(regex_part)

    regex_pattern = '\\.'.join(regex_pattern_parts)
    self.regex = re.compile(regex_pattern)

  def build_template(self):
    self.output_template = self.output_pattern.replace('<', '%(').replace('>', ')s')


def avg(values):
  if values:
    return float( sum(values) ) / len(values)


AGGREGATION_METHODS = {
  'sum' : sum,
  'avg' : avg,
}

# Importable singleton
RuleManager = RuleManager()
