
#!/usr/bin/env python
#coding: utf-8

# Input format: hash \t query \t region \t intent_probability \t urls list (json) \t layout (json) \t clicks (json)

import sys
import gc
import json
import math

from collections import defaultdict, namedtuple
from datetime import datetime

try:
	from config import *
except:
	from config_sample import *

REL_PRIORS = (0.5, 0.5)

DEFAULT_REL = REL_PRIORS[1] / sum(REL_PRIORS)

MAX_QUERY_ID = 1000	 # some initial value that will be updated by InputReader

SessionItem = namedtuple('SessionItem', ['intentWeight', 'query', 'urls', 'layout', 'clicks', 'extraclicks', 'exams'])

class ClickModel:

	def __init__(self, ignoreIntents=True, ignoreLayout=True):
		self.ignoreIntents = ignoreIntents
		self.ignoreLayout = ignoreLayout

	def train(self, sessions):
		"""
			Set some attributes that will be further used in _getClickProbs function
		"""
		pass

	def test(self, sessions, reportPositionPerplexity=True):
		logLikelihood = 0.0
		positionPerplexity = [0.0] * MAX_DOCS_PER_QUERY
		positionPerplexityClickSkip = [[0.0, 0.0] for i in xrange(MAX_DOCS_PER_QUERY)] #xrange is similar to range, but much faster
		counts = [0] * MAX_DOCS_PER_QUERY
		countsClickSkip = [[0, 0] for i in xrange(MAX_DOCS_PER_QUERY)]
		possibleIntents = [False] if self.ignoreIntents else [False, True]
		for s in sessions:
			iw = s.intentWeight
			intentWeight = {False: 1.0} if self.ignoreIntents else {False: 1 - iw, True: iw}
			clickProbs = self._getClickProbs(s, possibleIntents)
			N = len(s.clicks)
			
			#bound 
			for i in possibleIntents:
				for j in xrange(N):
					if clickProbs[i][j] == 0:
						clickProbs[i][j] = 0.00000001
					
			if DEBUG:
				assert N > 1
				x = sum(clickProbs[i][N // 2] * intentWeight[i] for i in possibleIntents) / sum(clickProbs[i][N // 2 - 1] * intentWeight[i] for i in possibleIntents)
				s.clicks[N // 2] = 1 if s.clicks[N // 2] == 0 else 0
				clickProbs2 = self._getClickProbs(s, possibleIntents)
				y = sum(clickProbs2[i][N // 2] * intentWeight[i] for i in possibleIntents) / sum(clickProbs2[i][N // 2 - 1] * intentWeight[i] for i in possibleIntents)
				assert abs(x + y - 1) < 0.01, (x, y)
			logLikelihood += math.log(sum(clickProbs[i][N - 1] * intentWeight[i] for i in possibleIntents))	  # log_e
			correctedRank = 0	# we are going to skip clicks on fake pager urls
			for k, click in enumerate(s.clicks):
				click = 1 if click else 0
				if s.extraclicks.get('TRANSFORMED', False) and (k + 1) % (SERP_SIZE + 1) == 0:
					if DEBUG:
						assert s.urls[k] == 'PAGER'
					continue
				# P(C_k | C_1, ..., C_{k-1}) = \sum_I P(C_1, ..., C_k | I) P(I) / \sum_I P(C_1, ..., C_{k-1} | I) P(I)
				curClick = dict((i, clickProbs[i][k]) for i in possibleIntents)
				prevClick = dict((i, clickProbs[i][k - 1]) for i in possibleIntents) if k > 0 else dict((i, 1.0) for i in possibleIntents)
				logProb = math.log(sum(curClick[i] * intentWeight[i] for i in possibleIntents), 2) - math.log(sum(prevClick[i] * intentWeight[i] for i in possibleIntents), 2)
				positionPerplexity[correctedRank] += logProb
				positionPerplexityClickSkip[correctedRank][click] += logProb
				counts[correctedRank] += 1
				countsClickSkip[correctedRank][click] += 1
				correctedRank += 1
		positionPerplexity = [2 ** (-x / count if count else x) for (x, count) in zip(positionPerplexity, counts)]
		positionPerplexityClickSkip = [[2 ** (-x[click] / (count[click] if count[click] else 1) if count else x) \
				for (x, count) in zip(positionPerplexityClickSkip, countsClickSkip)] for click in xrange(2)]
		perplexity = sum(positionPerplexity) / len(positionPerplexity)
		N = len(sessions)
		if reportPositionPerplexity:
			return logLikelihood / N / MAX_DOCS_PER_QUERY, perplexity, positionPerplexity, positionPerplexityClickSkip
		else:
			return logLikelihood / N / MAX_DOCS_PER_QUERY, perplexity

	def _getClickProbs(self, s, possibleIntents):
		"""
			Returns clickProbs list
			clickProbs[i][k] = P(C_1, ..., C_k | I=i)
		"""
		return dict((i, [0.5 ** (k + 1) for k in xrange(len(s.clicks))]) for i in possibleIntents)
	
	#def getRelevance(self, query_url_set):
	#	return {}


class DbnModel(ClickModel):

	def __init__(self, gammas, ignoreIntents=True, ignoreLayout=True):
		self.gammas = gammas
		ClickModel.__init__(self, ignoreIntents, ignoreLayout)

	def train(self, sessions):
		possibleIntents = [False] if self.ignoreIntents else [False, True]
		# intent -> query -> url -> (a_u, s_u)
		self.urlRelevances = dict((i, [defaultdict(lambda: {'a': DEFAULT_REL, 's': DEFAULT_REL}) for q in xrange(MAX_QUERY_ID)]) for i in possibleIntents)
		# here we store distribution of posterior intent weights given train data
		self.queryIntentsWeights = defaultdict(lambda: [])

		# EM algorithm
		if not PRETTY_LOG:
			print >>sys.stderr, '-' * 80
			print >>sys.stderr, 'Start. Current time is', datetime.now()
		for iteration_count in xrange(MAX_ITERATIONS):
			# urlRelFractions[intent][query][url][r][1] --- coefficient before \log r
			# urlRelFractions[intent][query][url][r][0] --- coefficient before \log (1 - r)
			urlRelFractions = dict((i, [defaultdict(lambda: {'a': [1.0, 1.0], 's': [1.0, 1.0]}) for q in xrange(MAX_QUERY_ID)]) for i in [False, True])	# set to store the parameters of Aquk, Squk
			self.queryIntentsWeights = defaultdict(lambda: [])
			# E step
			for s in sessions:
				#print(s.exams)
				positionRelevances = {} # set to store the parameters of Ak, Sk
				query = s.query
				for intent in possibleIntents:
					positionRelevances[intent] = {}
					for r in ['a', 's']:
						positionRelevances[intent][r] = [self.urlRelevances[intent][query][url][r] for url in s.urls]
				layout = [False] * len(s.layout) if self.ignoreLayout else s.layout
				sessionEstimate = dict((intent, self._getSessionEstimate(positionRelevances[intent], layout, s.clicks, intent)) for intent in possibleIntents)

				# P(I | C, G)
				if self.ignoreIntents:
					p_I__C_G = {False: 1, True: 0}
				else:
					a = sessionEstimate[False]['C'] * (1 - s.intentWeight)
					b = sessionEstimate[True]['C'] * s.intentWeight
					p_I__C_G = {False: a / (a + b), True: b / (a + b)}
				self.queryIntentsWeights[query].append(p_I__C_G[True])
				for k, url in enumerate(s.urls):
					for intent in possibleIntents:
						# update a
						urlRelFractions[intent][query][url]['a'][1] += sessionEstimate[intent]['a'][k] * p_I__C_G[intent] #the estimate times when 'url' is estimated as A_k = 1
						urlRelFractions[intent][query][url]['a'][0] += (1 - sessionEstimate[intent]['a'][k]) * p_I__C_G[intent]	#the estimate times when 'url' is estimated as A_k = 0
						if s.clicks[k] != 0:
							# Update s
							urlRelFractions[intent][query][url]['s'][1] += sessionEstimate[intent]['s'][k] * p_I__C_G[intent]	#the estimate times when 'url' is estimated as S_k = 1
							urlRelFractions[intent][query][url]['s'][0] += (1 - sessionEstimate[intent]['s'][k]) * p_I__C_G[intent]	#the estimate times when 'url' is estimated as S_k = 0
			if not PRETTY_LOG:
				sys.stderr.write('E')

			# M step
			# update parameters and record mean square error
			sum_square_displacement = 0.0
			Q_functional = 0.0
			num_points = 0
			for i in possibleIntents:
				for query, d in enumerate(urlRelFractions[i]):
					if not d:
						continue
					for url, relFractions in d.iteritems():
						a_u_new = relFractions['a'][1] / (relFractions['a'][1] + relFractions['a'][0]) #recalculate: P(A_K=1) = P(A_K=1)/(P(A_K=1)+P(A_K=0))
						sum_square_displacement += (a_u_new - self.urlRelevances[i][query][url]['a']) ** 2
						num_points += 1
						self.urlRelevances[i][query][url]['a'] = a_u_new
						Q_functional += relFractions['a'][1] * math.log(a_u_new) + relFractions['a'][0] * math.log(1 - a_u_new)
						s_u_new = relFractions['s'][1] / (relFractions['s'][1] + relFractions['s'][0])	#recalculate: P(S_K=1) = P(S_K=1)/(P(S_K=1)+P(S_K=0))
						sum_square_displacement += (s_u_new - self.urlRelevances[i][query][url]['s']) ** 2
						num_points += 1
						self.urlRelevances[i][query][url]['s'] = s_u_new
						Q_functional += relFractions['s'][1] * math.log(s_u_new) + relFractions['s'][0] * math.log(1 - s_u_new)
			if not PRETTY_LOG:
				sys.stderr.write('M\n')
			rmsd = math.sqrt(sum_square_displacement / (num_points if TRAIN_FOR_METRIC else 1.0))
			if PRETTY_LOG:
				sys.stderr.write('%d..' % (iteration_count + 1))
			else:
				print >>sys.stderr, 'Iteration: %d, RMSD: %.10f' % (iteration_count + 1, rmsd)
				print >>sys.stderr, 'Q functional: %f' % Q_functional
		if PRETTY_LOG:
			sys.stderr.write('\n')
		for q, intentWeights in self.queryIntentsWeights.iteritems():
			self.queryIntentsWeights[q] = sum(intentWeights) / len(intentWeights)

	@staticmethod
	def testBackwardForward():
		positionRelevances = {'a': [0.5] * MAX_DOCS_PER_QUERY, 's': [0.5] * MAX_DOCS_PER_QUERY}
		gammas = [0.9] * 4
		layout = [False] * (MAX_DOCS_PER_QUERY + 1)
		clicks = [0] * MAX_DOCS_PER_QUERY
		alpha, beta = DbnModel.getForwardBackwardEstimates(positionRelevances, gammas, layout, clicks, False)
		x = alpha[0][0] * beta[0][0] + alpha[0][1] * beta[0][1]
		assert all(abs((a[0] * b[0] + a[1] * b[1]) / x  - 1) < 0.00001 for a, b in zip(alpha, beta))

	@staticmethod
	def getGamma(gammas, k, layout, intent):
		index = 2 * (1 if layout[k + 1] else 0) + (1 if intent else 0)
		return gammas[index]

	@staticmethod
	def getForwardBackwardEstimates(positionRelevances, gammas, layout, clicks, intent):
		N = len(clicks)
		if DEBUG:
			assert N + 1 == len(layout)
		alpha = [[0.0, 0.0] for i in xrange(N + 1)]
		beta = [[0.0, 0.0] for i in xrange(N + 1)]
		alpha[0] = [0.0, 1.0]
		beta[N] = [1.0, 1.0]

		# P(E_{k+1} = e, C_k | E_k = e', G, I)
		updateMatrix = [[[0.0 for e1 in [0, 1]] for e in [0, 1]] for i in xrange(N)]
		for k, C_k in enumerate(clicks):
			a_u = positionRelevances['a'][k]
			s_u = positionRelevances['s'][k]
			gamma = DbnModel.getGamma(gammas, k, layout, intent)
			if C_k == 0:
				updateMatrix[k][0][0] = 1							#P(ek+1=0,ck=0|ek=0)		
				updateMatrix[k][0][1] = (1 - gamma) * (1 - a_u)		#P(ek+1=0,ck=0|ek=1)
				updateMatrix[k][1][0] = 0							#P(ek+1=1,ck=0|ek=0)
				updateMatrix[k][1][1] = gamma * (1 - a_u)			#P(ek+1=1,ck=0|ek=1)
			else:
				updateMatrix[k][0][0] = 0							#P(ek+1=0,ck=1|ek=0)
				updateMatrix[k][0][1] = (s_u + (1 - gamma) * (1 - s_u)) * a_u	#P(ek+1=0,ck=1|ek=1)
				updateMatrix[k][1][0] = 0							#P(ek+1=1,ck=1|ek=0)
				updateMatrix[k][1][1] = gamma * (1 - s_u) * a_u		#P(ek+1=1,ck=1|ek=1)

		for k in xrange(N):
			for e in [0, 1]:	#we may add predict examine here?
				alpha[k + 1][e] = sum(alpha[k][e1] * updateMatrix[k][e][e1] for e1 in [0, 1])	#forward to P(e{k+1}=e)
				beta[N - 1 - k][e] = sum(beta[N - k][e1] * updateMatrix[N - 1 - k][e1][e] for e1 in [0, 1])	#backward to P(e{k+1}=e)

		return alpha, beta

	def _getSessionEstimate(self, positionRelevances, layout, clicks, intent): #return the probability of occurence of this session
		# Returns {'a': P(A_k | I, C, G), 's': P(S_k | I, C, G), 'C': P(C | I, G), 'clicks': P(C_k | C_1, ..., C_{k-1}, I, G)} as a dict
		# sessionEstimate[True]['a'][k] = P(A_k = 1 | I = 'Fresh', C, G), probability of A_k = 0 can be calculated as 1 - p
		N = len(clicks)
		if DEBUG:
			assert N + 1 == len(layout)
		sessionEstimate = {'a': [0.0] * N, 's': [0.0] * N, 'e': [[0.0, 0.0] for k in xrange(N)], 'C': 0.0, 'clicks': [0.0] * N}

		alpha, beta = self.getForwardBackwardEstimates(positionRelevances, self.gammas, layout, clicks, intent)
		try:
			varphi = [((a[0] * b[0]) / (a[0] * b[0] + a[1] * b[1]), (a[1] * b[1]) / (a[0] * b[0] + a[1] * b[1])) for a, b in zip(alpha, beta)]
			#varphi[k] = (P(ek=0),P(ek=1))
		except ZeroDivisionError:
			print >>sys.stderr, alpha, beta, [(a[0] * b[0] + a[1] * b[1]) for a, b in zip(alpha, beta)], positionRelevances
			sys.exit(1)
		if DEBUG:
			assert all(ph[0] < 0.01 for ph, c in zip(varphi[:N], clicks) if c != 0), (alpha, beta, varphi, clicks)
		# calculate P(C | I, G) for k = 0
		sessionEstimate['C'] = alpha[0][0] * beta[0][0] + alpha[0][1] * beta[0][1]	  # == 0 + 1 * beta[0][1]
		#sessionEstimate['C'] = 1
		for k, C_k in enumerate(clicks):
			a_u = positionRelevances['a'][k]
			s_u = positionRelevances['s'][k]
			gamma = self.getGamma(self.gammas, k, layout, intent)
			# E_k_multiplier --- P(S_k = 0 | C_k) P(C_k | E_k = 1)
			if C_k == 0:
				sessionEstimate['a'][k] = a_u * varphi[k][0]
				sessionEstimate['s'][k] = 0.0
				#sessionEstimate['C'] = sessionEstimate['C'] * ((1 - a_u) * varphi[k][1] + varphi[k][0]) #new P(C | I, G)
			else:
				sessionEstimate['a'][k] = 1.0
				sessionEstimate['s'][k] = varphi[k + 1][0] * s_u / (s_u + (1 - gamma) * (1 - s_u))
				#sessionEstimate['C'] = sessionEstimate['C'] * (a_u * varphi[k][1]) #new P(C | I, G)
			# P(C_1, ..., C_k | I)  
			#this is the crucial parameter that is used to calculate LL & Perplexity
			sessionEstimate['clicks'][k] = sum(alpha[k + 1])
			'''
			if k == 0:
				sessionEstimate['clicks'][k] = C_k * (varphi[k][1]*a_u) + (1-C_k)*(varphi[k][0] + varphi[k][1]*(1-a_u))
			else:
				sessionEstimate['clicks'][k] = sessionEstimate['clicks'][k-1] * (C_k * (varphi[k][1]*a_u) + (1-C_k)*(varphi[k][0] + varphi[k][1]*(1-a_u)))
			'''
		return sessionEstimate

	def _getClickProbs(self, s, possibleIntents):
		"""
			Returns clickProbs list:
			clickProbs[i][k] = P(C_1, ..., C_k | I=i)
		"""
		# TODO: ensure that s.clicks[l] not used to calculate clickProbs[i][k] for l >= k
		positionRelevances = {}
		for intent in possibleIntents:
			positionRelevances[intent] = {}
			for r in ['a', 's']:
				positionRelevances[intent][r] = [self.urlRelevances[intent][s.query][url][r] for url in s.urls]
				if QUERY_INDEPENDENT_PAGER:
					for k, u in enumerate(s.urls):
						if u == 'PAGER':
							# use dummy 0 query for all fake pager URLs
							positionRelevances[intent][r][k] = self.urlRelevances[intent][0][url][r]
		layout = [False] * len(s.layout) if self.ignoreLayout else s.layout
		return dict((i, self._getSessionEstimate(positionRelevances[i], layout, s.clicks, i)['clicks']) for i in possibleIntents)

class SimplifiedDbnModel(DbnModel):

	def __init__(self, ignoreIntents=True, ignoreLayout=True):
		assert ignoreIntents
		assert ignoreLayout
		DbnModel.__init__(self, (1.0, 1.0, 1.0, 1.0), ignoreIntents, ignoreLayout)

	def train(self, sessions):
		urlRelFractions = [defaultdict(lambda: {'a': [1.0, 1.0], 's': [1.0, 1.0]}) for q in xrange(MAX_QUERY_ID)]
		for s in sessions:
			query = s.query
			lastClickedPos = len(s.clicks) - 1
			for k, c in enumerate(s.clicks):
				if c != 0:
					lastClickedPos = k
			for k, (u, c) in enumerate(zip(s.urls, s.clicks[:(lastClickedPos + 1)])):
				tmpQuery = query
				if QUERY_INDEPENDENT_PAGER and u == 'PAGER':
					assert TRANSFORM_LOG
					# the same dummy query for all pagers
					query = 0

				if c != 0:
					urlRelFractions[query][u]['a'][1] += 1
					if k == lastClickedPos:
						urlRelFractions[query][u]['s'][1] += 1
					else:
						urlRelFractions[query][u]['s'][0] += 1
				else:
					urlRelFractions[query][u]['a'][0] += 1
				if QUERY_INDEPENDENT_PAGER:
					query = tmpQuery
		self.urlRelevances = dict((i, [defaultdict(lambda: {'a': DEFAULT_REL, 's': DEFAULT_REL}) for q in xrange(MAX_QUERY_ID)]) for i in [False])
		for query, d in enumerate(urlRelFractions):
			if not d:
				continue
			for url, relFractions in d.iteritems():
				self.urlRelevances[False][query][url]['a'] = relFractions['a'][1] / (relFractions['a'][1] + relFractions['a'][0])
				self.urlRelevances[False][query][url]['s'] = relFractions['s'][1] / (relFractions['s'][1] + relFractions['s'][0])


class UbmModel(ClickModel):

	gammaTypesNum = 4

	def __init__(self, ignoreIntents=True, ignoreLayout=True, explorationBias=False):
		self.explorationBias = explorationBias
		ClickModel.__init__(self, ignoreIntents, ignoreLayout)

	def train(self, sessions):
		possibleIntents = [False] if self.ignoreIntents else [False, True]
		# alpha: intent -> query -> url -> "attractiveness probability"
		self.alpha = dict((i, [defaultdict(lambda: DEFAULT_REL) for q in xrange(MAX_QUERY_ID)]) for i in possibleIntents)
		# gamma: freshness of the current result: gammaType -> rank -> "distance from the last click" - 1 -> examination probability
		self.gamma = [[[0.5 for d in xrange(MAX_DOCS_PER_QUERY)] for r in xrange(MAX_DOCS_PER_QUERY)] for g in xrange(self.gammaTypesNum)]
		if self.explorationBias:
			self.e = [0.5 for p in xrange(MAX_DOCS_PER_QUERY)]
		if not PRETTY_LOG:
			print >>sys.stderr, '-' * 80
			print >>sys.stderr, 'Start. Current time is', datetime.now()
		for iteration_count in xrange(MAX_ITERATIONS):
			self.queryIntentsWeights = defaultdict(lambda: [])
			# not like in DBN! xxxFractions[0] is a numerator while xxxFraction[1] is a denominator
			alphaFractions = dict((i, [defaultdict(lambda: [1.0, 2.0]) for q in xrange(MAX_QUERY_ID)]) for i in possibleIntents)
			gammaFractions = [[[[1.0, 2.0] for d in xrange(MAX_DOCS_PER_QUERY)] for r in xrange(MAX_DOCS_PER_QUERY)] for g in xrange(self.gammaTypesNum)]
			if self.explorationBias:
				eFractions = [[1.0, 2.0] for p in xrange(MAX_DOCS_PER_QUERY)]
			# E-step
			for s in sessions:
				query = s.query
				layout = [False] * len(s.layout) if self.ignoreLayout else s.layout
				if self.explorationBias:
					explorationBiasPossible = any((l and c for (l, c) in zip(s.layout, s.clicks)))
					firstVerticalPos = -1 if not any(s.layout[:-1]) else [k for (k, l) in enumerate(s.layout) if l][0]
				if self.ignoreIntents:
					p_I__C_G = {False: 1.0, True: 0}
				else:
					a = self._getSessionProb(s) * (1 - s.intentWeight)
					b = 1 * s.intentWeight
					p_I__C_G = {False: a / (a + b), True: b / (a + b)}
				self.queryIntentsWeights[query].append(p_I__C_G[True])
				prevClick = -1
				for rank, c in enumerate(s.clicks):
					url = s.urls[rank]
					for intent in possibleIntents:
						a = self.alpha[intent][query][url]
						if self.explorationBias and explorationBiasPossible:
							e = self.e[firstVerticalPos]
						if c == 0:
							g = self.getGamma(self.gamma, rank, prevClick, layout, intent)
							gCorrection = 1
							if self.explorationBias and explorationBiasPossible and not s.layout[k]:
								gCorrection = 1 - e
								g *= gCorrection
							alphaFractions[intent][query][url][0] += a * (1 - g) / (1 - a * g) * p_I__C_G[intent]
							self.getGamma(gammaFractions, rank, prevClick, layout, intent)[0] += g / gCorrection * (1 - a) / (1 - a * g) * p_I__C_G[intent]
							if self.explorationBias and explorationBiasPossible:
								eFractions[firstVerticalPos][0] += (e if s.layout[k] else e / (1 - a * g)) * p_I__C_G[intent]
						else:
							alphaFractions[intent][query][url][0] += 1 * p_I__C_G[intent]
							self.getGamma(gammaFractions, rank, prevClick, layout, intent)[0] += 1 * p_I__C_G[intent]
							if self.explorationBias and explorationBiasPossible:
								eFractions[firstVerticalPos][0] += (e if s.layout[k] else 0) * p_I__C_G[intent]
						alphaFractions[intent][query][url][1] += 1 * p_I__C_G[intent]
						self.getGamma(gammaFractions, rank, prevClick, layout, intent)[1] += 1 * p_I__C_G[intent]
						if self.explorationBias and explorationBiasPossible:
							eFractions[firstVerticalPos][1] += 1 * p_I__C_G[intent]
					if c != 0:
						prevClick = rank
			if not PRETTY_LOG:
				sys.stderr.write('E')
			# M-step
			sum_square_displacement = 0.0
			num_points = 0
			for i in possibleIntents:
				for q in xrange(MAX_QUERY_ID):
					for url, aF in alphaFractions[i][q].iteritems():
						new_alpha = aF[0] / aF[1]
						sum_square_displacement += (self.alpha[i][q][url] - new_alpha) ** 2
						num_points += 1
						self.alpha[i][q][url] = new_alpha
			for g in xrange(self.gammaTypesNum):
				for r in xrange(MAX_DOCS_PER_QUERY):
					for d in xrange(MAX_DOCS_PER_QUERY):
						gF = gammaFractions[g][r][d]
						new_gamma = gF[0] / gF[1]
						sum_square_displacement += (self.gamma[g][r][d] - new_gamma) ** 2
						num_points += 1
						self.gamma[g][r][d] = new_gamma
			if self.explorationBias:
				for p in xrange(MAX_DOCS_PER_QUERY):
					new_e = eFractions[p][0] / eFractions[p][1]
					sum_square_displacement += (self.e[p] - new_e) ** 2
					num_points += 1
					self.e[p] = new_e
			if not PRETTY_LOG:
				sys.stderr.write('M\n')
			rmsd = math.sqrt(sum_square_displacement / (num_points if TRAIN_FOR_METRIC else 1.0))
			if PRETTY_LOG:
				sys.stderr.write('%d..' % (iteration_count + 1))
			else:
				print >>sys.stderr, 'Iteration: %d, RMSD: %.10f' % (iteration_count + 1, rmsd)
		if PRETTY_LOG:
			sys.stderr.write('\n')
		for q, intentWeights in self.queryIntentsWeights.iteritems():
			self.queryIntentsWeights[q] = sum(intentWeights) / len(intentWeights)

	def _getSessionProb(self, s):
		clickProbs = self._getClickProbs(s, [False, True])
		N = len(s.clicks)
		return clickProbs[False][N - 1] / clickProbs[True][N - 1]

	@staticmethod
	def getGamma(gammas, k, prevClick, layout, intent):
		index = (2 if layout[k] else 0) + (1 if intent else 0)
		return gammas[index][k][k - prevClick - 1]

	def _getClickProbs(self, s, possibleIntents):
		"""
			Returns clickProbs list
			clickProbs[i][k] = P(C_1, ..., C_k | I=i)
		"""
		clickProbs = dict((i, []) for i in possibleIntents)
		firstVerticalPos = -1 if not any(s.layout[:-1]) else [k for (k, l) in enumerate(s.layout) if l][0]
		prevClick = -1
		layout = [False] * len(s.layout) if self.ignoreLayout else s.layout
		for rank, c in enumerate(s.clicks):
			url = s.urls[rank]
			prob = {False: 0.0, True: 0.0}
			for i in possibleIntents:
				a = self.alpha[i][s.query][url]
				g = self.getGamma(self.gamma, rank, prevClick, layout, i)
				if self.explorationBias and any(s.layout[k] and s.clicks[k] for k in xrange(rank)) and not s.layout[rank]:
					g *= 1 - self.e[firstVerticalPos]
				prevProb = 1 if rank == 0 else clickProbs[i][-1]
				if c == 0:
					clickProbs[i].append(prevProb * (1 - a * g))
				else:
					clickProbs[i].append(prevProb * a * g)
			if c != 0:
				prevClick = rank
		return clickProbs

	def getRelSet(self):
		rel_set = {}
		for q in xrange(len(self.alpha[False])):
			rel_set[q] = {}
			for u in self.alpha[False][q]:
				rel_set[q][u] = self.alpha[False][q][u]
		return rel_set 

class EbUbmModel(UbmModel):
	def __init__(self, ignoreIntents=True, ignoreLayout=True):
		UbmModel.__init__(self, ignoreIntents, ignoreLayout, explorationBias=True)


class DcmModel(ClickModel):

	gammaTypesNum = 4

	def train(self, sessions):
		possibleIntents = [False] if self.ignoreIntents else [False, True]
		urlRelFractions = dict((i, [defaultdict(lambda: [1.0, 1.0]) for q in xrange(MAX_QUERY_ID)]) for i in possibleIntents)
		gammaFractions = [[[1.0, 1.0] for g in xrange(self.gammaTypesNum)] for r in xrange(MAX_DOCS_PER_QUERY)]
		for s in sessions:
			query = s.query
			layout = [False] * len(s.layout) if self.ignoreLayout else s.layout
			lastClickedPos = MAX_DOCS_PER_QUERY - 1
			for k, c in enumerate(s.clicks):
				if c != 0:
					lastClickedPos = k
			intentWeights = {False: 1.0} if self.ignoreIntents else {False: 1 - s.intentWeight, True: s.intentWeight}
			for k, (u, c) in enumerate(zip(s.urls, s.clicks[:(lastClickedPos + 1)])):
				for i in possibleIntents:
					if c != 0:
						urlRelFractions[i][query][u][1] += intentWeights[i]
						if k == lastClickedPos:
							self.getGamma(gammaFractions[k], k, layout, i)[1] += intentWeights[i]
						else:
							self.getGamma(gammaFractions[k], k, layout, i)[0] += intentWeights[i]
					else:
						urlRelFractions[i][query][u][0] += intentWeights[i]
		self.urlRelevances = dict((i, [defaultdict(lambda: DEFAULT_REL) for q in xrange(MAX_QUERY_ID)]) for i in possibleIntents)
		self.gammas = [[0.5 for g in xrange(self.gammaTypesNum)] for r in xrange(MAX_DOCS_PER_QUERY)]
		for i in possibleIntents:
			for query, d in enumerate(urlRelFractions[i]):
				if not d:
					continue
				for url, relFractions in d.iteritems():
					self.urlRelevances[i][query][url] = relFractions[1] / (relFractions[1] + relFractions[0])
		for k in xrange(MAX_DOCS_PER_QUERY):
			for g in xrange(self.gammaTypesNum):
				self.gammas[k][g] = gammaFractions[k][g][0] / (gammaFractions[k][g][0] + gammaFractions[k][g][1])

	def _getClickProbs(self, s, possibleIntents):
		clickProbs = {False: [], True: []}		  # P(C_1, ..., C_k)
		query = s.query
		layout = [False] * len(s.layout) if self.ignoreLayout else s.layout
		for i in possibleIntents:
			examinationProb = 1.0	   # P(C_1, ..., C_{k - 1}, E_k = 1)
			for k, c in enumerate(s.clicks):
				r = self.urlRelevances[i][query][s.urls[k]]
				prevProb = 1 if k == 0 else clickProbs[i][-1]
				if c == 0:
					clickProbs[i].append(prevProb - examinationProb * r)	# P(C_1, ..., C_k = 0) = P(C_1, ..., C_{k-1}) - P(C_1, ..., C_k = 1)
					examinationProb *= 1 - r								# P(C_1, ..., C_k, E_{k+1} = 1) = P(E_{k+1} = 1 | C_k, E_k = 1) * P(C_k | E_k = 1) *  P(C_1, ..., C_{k - 1}, E_k = 1)
				else:
					clickProbs[i].append(examinationProb * r)
					examinationProb *= self.getGamma(self.gammas[k], k, layout, i) * r  # P(C_1, ..., C_k, E_{k+1} = 1) = P(E_{k+1} = 1 | C_k, E_k = 1) * P(C_k | E_k = 1) *  P(C_1, ..., C_{k - 1}, E_k = 1)
					

		return clickProbs

	@staticmethod
	def getGamma(gammas, k, layout, intent):
		return DbnModel.getGamma(gammas, k, layout, intent)

class NaiveModel(ClickModel):

	def __init__(self, ignoreExamInProb, ignoreExamInCTR, ignoreIntents=True, ignoreLayout=True):
		self.ignoreExamInProb = ignoreExamInProb
		self.ignoreExamInCTR = ignoreExamInCTR
		ClickModel.__init__(self, ignoreIntents, ignoreLayout)
	
	gammaTypesNum = 4

	def train(self, sessions):
		possibleIntents = [False] if self.ignoreIntents else [False, True]
		urlRelFractions = dict((i, [defaultdict(lambda: [1.0, 1.0]) for q in xrange(MAX_QUERY_ID)]) for i in possibleIntents)
		print('session = ' + str(len(sessions)))
		for s in sessions:
			query = s.query
			layout = [False] * len(s.layout) if self.ignoreLayout else s.layout
			intentWeights = {False: 1.0} if self.ignoreIntents else {False: 1 - s.intentWeight, True: s.intentWeight}
			#for k, (u, c) in enumerate(zip(s.urls, s.clicks[:(lastClickedPos + 1)])):
			for k, (u, c) in enumerate(zip(s.urls, s.clicks)):
				for i in possibleIntents:
					if c != 0:
						urlRelFractions[i][query][u][1] += intentWeights[i]
					else:
						exam = 1.0 if self.ignoreExamInCTR else s.exams[k]
						urlRelFractions[i][query][u][0] += intentWeights[i]*exam
		self.urlRelevances = dict((i, [defaultdict(lambda: DEFAULT_REL) for q in xrange(MAX_QUERY_ID)]) for i in possibleIntents)
		for i in possibleIntents:
			for query, d in enumerate(urlRelFractions[i]):
				if not d:
					continue
				for url, relFractions in d.iteritems():
					#print(str(url) + " : " + str(relFractions[0]))
					self.urlRelevances[i][query][url] = relFractions[1] / (relFractions[1] + relFractions[0])
		#print("relevance" + str(self.urlRelevances[False]))

	def _getClickProbs(self, s, possibleIntents):
		clickProbs = {False: [], True: []}		  # P(C_1, ..., C_k)
		query = s.query
		layout = [False] * len(s.layout) if self.ignoreLayout else s.layout
		for i in possibleIntents:
			examinationProb = 1.0	   # P(C_1, ..., C_{k - 1}, E_k = 1)
			for k, c in enumerate(s.clicks):
				r = self.urlRelevances[i][query][s.urls[k]]
				prevProb = 1 if k == 0 else clickProbs[i][-1]
				exam = 1 if self.ignoreExamInProb else s.exams[k]
				if c == 0:
					clickProbs[i].append(prevProb * (1 - exam * r))	# P(C_1, ..., C_k = 0) = P(C_1, ..., C_{k-1}) - P(C_1, ..., C_k = 1)
				else:
					clickProbs[i].append(prevProb * exam * r)
		for i in possibleIntents:
			for j in range(0,len(clickProbs[i])):
				if clickProbs[i][j] <= 0:
					clickProbs[i][j] = 0.00000000000000000000001
		return clickProbs

	@staticmethod
	def getGamma(gammas, k, layout, intent):
		return DbnModel.getGamma(gammas, k, layout, intent)
	
	def getRelevance(self, query_url_set, readInput):
		rel_set = {}
		count = 0
		for query in query_url_set:
			try:
				q_id = readInput.query_to_id[(query,readInput.region)]
				rel_set[query] = {}
				for url in query_url_set[query]:
					u_id = readInput.url_to_id[url]
					if self.urlRelevances[False][q_id].has_key(u_id):
						rel_set[query][url] = self.urlRelevances[False][q_id][u_id]
			except:
				continue
		#print('match ' + str(count) + ' ' + str(len(rel_set)))
		return rel_set
	
	def getRelSet(self):
		rel_set = {}
		for q in xrange(len(self.urlRelevances[False])):
			rel_set[q] = {}
			for u in self.urlRelevances[False][q]:
				rel_set[q][u] = self.urlRelevances[False][q][u]
		return rel_set 

class MouseDbnModel(ClickModel):

	def __init__(self, gammas, erate, prate, ignoreIntents=True, ignoreLayout=True):
		self.gammas = gammas
		self.erate = erate
		self.prate = prate
		print(str(self.erate) + ' : ' + str(self.prate))
		ClickModel.__init__(self, ignoreIntents, ignoreLayout)

	def train(self, sessions):
		possibleIntents = [False] if self.ignoreIntents else [False, True]
		# intent -> query -> url -> (a_u, s_u)
		self.urlRelevances = dict((i, [defaultdict(lambda: {'a': DEFAULT_REL, 's': DEFAULT_REL}) for q in xrange(MAX_QUERY_ID)]) for i in possibleIntents)
		# here we store distribution of posterior intent weights given train data
		self.queryIntentsWeights = defaultdict(lambda: [])

		# EM algorithm
		if not PRETTY_LOG:
			print >>sys.stderr, '-' * 80
			print >>sys.stderr, 'Start. Current time is', datetime.now()
		for iteration_count in xrange(MAX_ITERATIONS):
			# urlRelFractions[intent][query][url][r][1] --- coefficient before \log r
			# urlRelFractions[intent][query][url][r][0] --- coefficient before \log (1 - r)
			urlRelFractions = dict((i, [defaultdict(lambda: {'a': [1.0, 1.0], 's': [1.0, 1.0]}) for q in xrange(MAX_QUERY_ID)]) for i in [False, True])	# set to store the parameters of Aquk, Squk
			self.queryIntentsWeights = defaultdict(lambda: [])
			# E step
			for s in sessions:
				#print(s.exams)
				positionRelevances = {} # set to store the parameters of Ak, Sk
				query = s.query
				for intent in possibleIntents:
					positionRelevances[intent] = {}
					for r in ['a', 's']:
						positionRelevances[intent][r] = [self.urlRelevances[intent][query][url][r] for url in s.urls]
				layout = [False] * len(s.layout) if self.ignoreLayout else s.layout
				sessionEstimate = dict((intent, self._getSessionEstimate(positionRelevances[intent], layout, s.clicks, s.exams, self.erate, intent)) for intent in possibleIntents)

				# P(I | C, G)
				if self.ignoreIntents:
					p_I__C_G = {False: 1, True: 0}
				else:
					a = sessionEstimate[False]['C'] * (1 - s.intentWeight)
					b = sessionEstimate[True]['C'] * s.intentWeight
					p_I__C_G = {False: a / (a + b), True: b / (a + b)}
				self.queryIntentsWeights[query].append(p_I__C_G[True])
				for k, url in enumerate(s.urls):
					for intent in possibleIntents:
						# update a
						urlRelFractions[intent][query][url]['a'][1] += sessionEstimate[intent]['a'][k] * p_I__C_G[intent] #the estimate times when 'url' is estimated as A_k = 1
						urlRelFractions[intent][query][url]['a'][0] += (1 - sessionEstimate[intent]['a'][k]) * p_I__C_G[intent]	#the estimate times when 'url' is estimated as A_k = 0
						if s.clicks[k] != 0:
							# Update s
							urlRelFractions[intent][query][url]['s'][1] += sessionEstimate[intent]['s'][k] * p_I__C_G[intent]	#the estimate times when 'url' is estimated as S_k = 1
							urlRelFractions[intent][query][url]['s'][0] += (1 - sessionEstimate[intent]['s'][k]) * p_I__C_G[intent]	#the estimate times when 'url' is estimated as S_k = 0
			if not PRETTY_LOG:
				sys.stderr.write('E')

			# M step
			# update parameters and record mean square error
			sum_square_displacement = 0.0
			Q_functional = 0.0
			num_points = 0
			for i in possibleIntents:
				for query, d in enumerate(urlRelFractions[i]):
					if not d:
						continue
					for url, relFractions in d.iteritems():
						a_u_new = relFractions['a'][1] / (relFractions['a'][1] + relFractions['a'][0]) #recalculate: P(A_K=1) = P(A_K=1)/(P(A_K=1)+P(A_K=0))
						sum_square_displacement += (a_u_new - self.urlRelevances[i][query][url]['a']) ** 2
						num_points += 1
						self.urlRelevances[i][query][url]['a'] = a_u_new
						Q_functional += relFractions['a'][1] * math.log(a_u_new) + relFractions['a'][0] * math.log(1 - a_u_new)
						s_u_new = relFractions['s'][1] / (relFractions['s'][1] + relFractions['s'][0])	#recalculate: P(S_K=1) = P(S_K=1)/(P(S_K=1)+P(S_K=0))
						sum_square_displacement += (s_u_new - self.urlRelevances[i][query][url]['s']) ** 2
						num_points += 1
						self.urlRelevances[i][query][url]['s'] = s_u_new
						Q_functional += relFractions['s'][1] * math.log(s_u_new) + relFractions['s'][0] * math.log(1 - s_u_new)
			if not PRETTY_LOG:
				sys.stderr.write('M\n')
			rmsd = math.sqrt(sum_square_displacement / (num_points if TRAIN_FOR_METRIC else 1.0))
			if PRETTY_LOG:
				sys.stderr.write('%d..' % (iteration_count + 1))
			else:
				print >>sys.stderr, 'Iteration: %d, RMSD: %.10f' % (iteration_count + 1, rmsd)
				print >>sys.stderr, 'Q functional: %f' % Q_functional
		if PRETTY_LOG:
			sys.stderr.write('\n')
		for q, intentWeights in self.queryIntentsWeights.iteritems():
			self.queryIntentsWeights[q] = sum(intentWeights) / len(intentWeights)

	@staticmethod
	def testBackwardForward():
		positionRelevances = {'a': [0.5] * MAX_DOCS_PER_QUERY, 's': [0.5] * MAX_DOCS_PER_QUERY}
		gammas = [0.9] * 4
		layout = [False] * (MAX_DOCS_PER_QUERY + 1)
		clicks = [0] * MAX_DOCS_PER_QUERY
		alpha, beta = MouseDbnModel.getForwardBackwardEstimates(positionRelevances, gammas, layout, clicks, exams, False)
		x = alpha[0][0] * beta[0][0] + alpha[0][1] * beta[0][1]
		assert all(abs((a[0] * b[0] + a[1] * b[1]) / x  - 1) < 0.00001 for a, b in zip(alpha, beta))

	@staticmethod
	def getGamma(gammas, k, layout, intent):
		index = 2 * (1 if layout[k + 1] else 0) + (1 if intent else 0)
		return gammas[index]

	@staticmethod
	def getForwardBackwardEstimates(positionRelevances, gammas, layout, clicks, exams, rate, intent):
		N = len(clicks)
		if DEBUG:
			assert N + 1 == len(layout)
		alpha = [[0.0, 0.0] for i in xrange(N + 1)]
		beta = [[0.0, 0.0] for i in xrange(N + 1)]
		alpha[0] = [0.0, 1.0]
		beta[N] = [1.0, 1.0]

		# P(E_{k+1} = e, C_k | E_k = e', G, I)
		updateMatrix = [[[0.0 for e1 in [0, 1]] for e in [0, 1]] for i in xrange(N)]
		for k, C_k in enumerate(clicks):
			a_u = positionRelevances['a'][k]
			s_u = positionRelevances['s'][k]
			gamma = MouseDbnModel.getGamma(gammas, k, layout, intent)
			#add mouse data
			if k+1 < N:
				gamma = gamma * rate + exams[k+1] * (1-rate) #add mouse predict exam
			if gamma == 0:
				gamma += 0.000000001
			if C_k == 0:
				updateMatrix[k][0][0] = 1							#P(ek+1=0,ck=0|ek=0)		
				updateMatrix[k][0][1] = (1 - gamma) * (1 - a_u)		#P(ek+1=0,ck=0|ek=1)
				updateMatrix[k][1][0] = 0							#P(ek+1=1,ck=0|ek=0)
				updateMatrix[k][1][1] = gamma * (1 - a_u)			#P(ek+1=1,ck=0|ek=1)
			else:
				updateMatrix[k][0][0] = 0							#P(ek+1=0,ck=1|ek=0)
				updateMatrix[k][0][1] = (s_u + (1 - gamma) * (1 - s_u)) * a_u	#P(ek+1=0,ck=1|ek=1)
				updateMatrix[k][1][0] = 0							#P(ek+1=1,ck=1|ek=0)
				updateMatrix[k][1][1] = gamma * (1 - s_u) * a_u		#P(ek+1=1,ck=1|ek=1)

		for k in xrange(N):
			for e in [0, 1]:	#we may add predict examine here?
				alpha[k + 1][e] = sum(alpha[k][e1] * updateMatrix[k][e][e1] for e1 in [0, 1])	#forward to P(e{k+1}=e)
				beta[N - 1 - k][e] = sum(beta[N - k][e1] * updateMatrix[N - 1 - k][e1][e] for e1 in [0, 1])	#backward to P(e{k+1}=e)

		return alpha, beta

	def _getSessionEstimate(self, positionRelevances, layout, clicks, exams, rate, intent): #return the probability of occurence of this session
		# Returns {'a': P(A_k | I, C, G), 's': P(S_k | I, C, G), 'C': P(C | I, G), 'clicks': P(C_k | C_1, ..., C_{k-1}, I, G)} as a dict
		# sessionEstimate[True]['a'][k] = P(A_k = 1 | I = 'Fresh', C, G), probability of A_k = 0 can be calculated as 1 - p
		N = len(clicks)
		if DEBUG:
			assert N + 1 == len(layout)
		sessionEstimate = {'a': [0.0] * N, 's': [0.0] * N, 'e': [[0.0, 0.0] for k in xrange(N)], 'C': 0.0, 'clicks': [0.0] * N}

		alpha, beta = self.getForwardBackwardEstimates(positionRelevances, self.gammas, layout, clicks,exams,rate, intent)
		try:
			#varphi[k] = (P(ek=0),P(ek=1))
			varphi = [((a[0] * b[0]) / (a[0] * b[0] + a[1] * b[1]), (a[1] * b[1]) / (a[0] * b[0] + a[1] * b[1])) for a, b in zip(alpha, beta)]
			'''
			for k, E_k in enumerate(exams):
				if k >= len(clicks):
					break
				varphi[k] = (self.rate * varphi[k][0] + (1 - self.rate) * (1 - E_k), self.rate * varphi[k][1] + (1 - self.rate) * (E_k))
			'''
		except ZeroDivisionError:
			print >>sys.stderr, alpha, beta, [(a[0] * b[0] + a[1] * b[1]) for a, b in zip(alpha, beta)], positionRelevances
			sys.exit(1)
		if DEBUG:
			assert all(ph[0] < 0.01 for ph, c in zip(varphi[:N], clicks) if c != 0), (alpha, beta, varphi, clicks)
		# calculate P(C | I, G) for k = 0
		sessionEstimate['C'] = alpha[0][0] * beta[0][0] + alpha[0][1] * beta[0][1]	  # == 0 + 1 * beta[0][1]
		#sessionEstimate['C'] = 1
		for k, C_k in enumerate(clicks):
			a_u = positionRelevances['a'][k]
			s_u = positionRelevances['s'][k]
			gamma = self.getGamma(self.gammas, k, layout, intent)
			# E_k_multiplier --- P(S_k = 0 | C_k) P(C_k | E_k = 1)
			if C_k == 0:
				sessionEstimate['a'][k] = a_u * varphi[k][0]
				sessionEstimate['s'][k] = 0.0
				#sessionEstimate['C'] = sessionEstimate['C'] * ((1 - a_u) * varphi[k][1] + varphi[k][0]) #new P(C | I, G)
			else:
				sessionEstimate['a'][k] = 1.0
				sessionEstimate['s'][k] = varphi[k + 1][0] * s_u / (s_u + (1 - gamma) * (1 - s_u))
				#sessionEstimate['C'] = sessionEstimate['C'] * (a_u * varphi[k][1]) #new P(C | I, G)
			# P(C_1, ..., C_k | I)  
			#this is the crucial parameter that is used to calculate LL & Perplexity
			sessionEstimate['clicks'][k] = sum(alpha[k + 1])
			'''
			if C_k != 0 and C_k != 1:
				print("ERROR")
			if k == 0:
				sessionEstimate['clicks'][k] = C_k * (varphi[k][1]*a_u) + (1-C_k)*(varphi[k][0] + varphi[k][1]*(1-a_u))
			else:
				sessionEstimate['clicks'][k] = sessionEstimate['clicks'][k-1] * (C_k * (varphi[k][1]*a_u) + (1-C_k)*(varphi[k][0] + varphi[k][1]*(1-a_u)))
			'''
		return sessionEstimate

	def _getClickProbs(self, s, possibleIntents):
		"""
			Returns clickProbs list:
			clickProbs[i][k] = P(C_1, ..., C_k | I=i)
		"""
		# TODO: ensure that s.clicks[l] not used to calculate clickProbs[i][k] for l >= k
		positionRelevances = {}
		for intent in possibleIntents:
			positionRelevances[intent] = {}
			for r in ['a', 's']:
				positionRelevances[intent][r] = [self.urlRelevances[intent][s.query][url][r] for url in s.urls]
				if QUERY_INDEPENDENT_PAGER:
					for k, u in enumerate(s.urls):
						if u == 'PAGER':
							# use dummy 0 query for all fake pager URLs
							positionRelevances[intent][r][k] = self.urlRelevances[intent][0][url][r]
		layout = [False] * len(s.layout) if self.ignoreLayout else s.layout
		return dict((i, self._getSessionEstimate(positionRelevances[i], layout, s.clicks, s.exams, self.prate, i)['clicks']) for i in possibleIntents)
	
	def getRelevance(self, query_url_set, readInput):
		rel_set = {}
		count = 0
		for query in query_url_set:
			try:
				q_id = readInput.query_to_id[(query,readInput.region)]
				rel_set[query] = {}
				for url in query_url_set[query]:
					u_id = readInput.url_to_id[url]
					if self.urlRelevances[False][q_id].has_key(u_id):
						rel_set[query][url] = self.urlRelevances[False][q_id][u_id]['s']
			except:
				continue
		#print('match ' + str(count) + ' ' + str(len(rel_set)))
		return rel_set
	
	def getRelSet(self):
		rel_set = {}
		for q in xrange(len(self.urlRelevances[False])):
			rel_set[q] = {}
			for u in self.urlRelevances[False][q]:
				rel_set[q][u] = self.urlRelevances[False][q][u]['s']
		return rel_set 
	
class MouseUbmModel(ClickModel):

	gammaTypesNum = 4

	def __init__(self, erate, prate, ignoreIntents=True, ignoreLayout=True, explorationBias=False):
		self.explorationBias = explorationBias
		self.erate = erate
		self.prate = prate
		print(str(self.erate) + ' : ' + str(self.prate))
		ClickModel.__init__(self, ignoreIntents, ignoreLayout)

	def train(self, sessions):
		possibleIntents = [False] if self.ignoreIntents else [False, True]
		# alpha: intent -> query -> url -> "attractiveness probability"
		self.alpha = dict((i, [defaultdict(lambda: DEFAULT_REL) for q in xrange(MAX_QUERY_ID)]) for i in possibleIntents)
		# gamma: freshness of the current result: gammaType -> rank -> "distance from the last click" - 1 -> examination probability
		self.gamma = [[[0.5 for d in xrange(MAX_DOCS_PER_QUERY)] for r in xrange(MAX_DOCS_PER_QUERY)] for g in xrange(self.gammaTypesNum)]
		if self.explorationBias:
			self.e = [0.5 for p in xrange(MAX_DOCS_PER_QUERY)]
		if not PRETTY_LOG:
			print >>sys.stderr, '-' * 80
			print >>sys.stderr, 'Start. Current time is', datetime.now()
		for iteration_count in xrange(MAX_ITERATIONS):
			self.queryIntentsWeights = defaultdict(lambda: [])
			# not like in DBN! xxxFractions[0] is a numerator while xxxFraction[1] is a denominator
			alphaFractions = dict((i, [defaultdict(lambda: [1.0, 2.0]) for q in xrange(MAX_QUERY_ID)]) for i in possibleIntents)
			gammaFractions = [[[[1.0, 2.0] for d in xrange(MAX_DOCS_PER_QUERY)] for r in xrange(MAX_DOCS_PER_QUERY)] for g in xrange(self.gammaTypesNum)]
			if self.explorationBias:
				eFractions = [[1.0, 2.0] for p in xrange(MAX_DOCS_PER_QUERY)]
			# E-step
			for s in sessions:
				query = s.query
				layout = [False] * len(s.layout) if self.ignoreLayout else s.layout
				if self.explorationBias:
					explorationBiasPossible = any((l and c for (l, c) in zip(s.layout, s.clicks)))
					firstVerticalPos = -1 if not any(s.layout[:-1]) else [k for (k, l) in enumerate(s.layout) if l][0]
				if self.ignoreIntents:
					p_I__C_G = {False: 1.0, True: 0}
				else:
					a = self._getSessionProb(s) * (1 - s.intentWeight)
					b = 1 * s.intentWeight
					p_I__C_G = {False: a / (a + b), True: b / (a + b)}
				self.queryIntentsWeights[query].append(p_I__C_G[True])
				prevClick = -1
				for rank, c in enumerate(s.clicks):
					url = s.urls[rank]
					for intent in possibleIntents:
						a = self.alpha[intent][query][url]
						if self.explorationBias and explorationBiasPossible:
							e = self.e[firstVerticalPos]
						if c == 0:
							g = self.getGamma(self.gamma, s.exams, rank, prevClick, layout, intent)
							#add mouse data
							#print('line 805 g = '+str(g))
							g = g * self.erate + s.exams[rank] * (1-self.erate)
							gCorrection = 1
							if self.explorationBias and explorationBiasPossible and not s.layout[k]:
								gCorrection = 1 - e
								g *= gCorrection
							alphaFractions[intent][query][url][0] += a * (1 - g) / (1 - a * g) * p_I__C_G[intent]
							self.getGamma(gammaFractions, s.exams, rank, prevClick, layout, intent)[0] += g / gCorrection * (1 - a) / (1 - a * g) * p_I__C_G[intent]
							if self.explorationBias and explorationBiasPossible:
								eFractions[firstVerticalPos][0] += (e if s.layout[k] else e / (1 - a * g)) * p_I__C_G[intent]
						else:
							alphaFractions[intent][query][url][0] += 1 * p_I__C_G[intent]
							self.getGamma(gammaFractions, s.exams, rank, prevClick, layout, intent)[0] += 1 * p_I__C_G[intent]
							if self.explorationBias and explorationBiasPossible:
								eFractions[firstVerticalPos][0] += (e if s.layout[k] else 0) * p_I__C_G[intent]
						alphaFractions[intent][query][url][1] += 1 * p_I__C_G[intent]
						self.getGamma(gammaFractions, s.exams, rank, prevClick, layout, intent)[1] += 1 * p_I__C_G[intent]
						if self.explorationBias and explorationBiasPossible:
							eFractions[firstVerticalPos][1] += 1 * p_I__C_G[intent]
					if c != 0:
						prevClick = rank
			if not PRETTY_LOG:
				sys.stderr.write('E')
			# M-step
			sum_square_displacement = 0.0
			num_points = 0
			for i in possibleIntents:
				for q in xrange(MAX_QUERY_ID):
					for url, aF in alphaFractions[i][q].iteritems():
						new_alpha = aF[0] / aF[1]
						sum_square_displacement += (self.alpha[i][q][url] - new_alpha) ** 2
						num_points += 1
						self.alpha[i][q][url] = new_alpha
			for g in xrange(self.gammaTypesNum):
				for r in xrange(MAX_DOCS_PER_QUERY):
					for d in xrange(MAX_DOCS_PER_QUERY):
						gF = gammaFractions[g][r][d]
						new_gamma = gF[0] / gF[1]
						sum_square_displacement += (self.gamma[g][r][d] - new_gamma) ** 2
						num_points += 1
						self.gamma[g][r][d] = new_gamma
			if self.explorationBias:
				for p in xrange(MAX_DOCS_PER_QUERY):
					new_e = eFractions[p][0] / eFractions[p][1]
					sum_square_displacement += (self.e[p] - new_e) ** 2
					num_points += 1
					self.e[p] = new_e
			if not PRETTY_LOG:
				sys.stderr.write('M\n')
			rmsd = math.sqrt(sum_square_displacement / (num_points if TRAIN_FOR_METRIC else 1.0))
			if PRETTY_LOG:
				sys.stderr.write('%d..' % (iteration_count + 1))
			else:
				print >>sys.stderr, 'Iteration: %d, RMSD: %.10f' % (iteration_count + 1, rmsd)
		if PRETTY_LOG:
			sys.stderr.write('\n')
		for q, intentWeights in self.queryIntentsWeights.iteritems():
			self.queryIntentsWeights[q] = sum(intentWeights) / len(intentWeights)

	def _getSessionProb(self, s):
		clickProbs = self._getClickProbs(s, [False, True])
		N = len(s.clicks)
		return clickProbs[False][N - 1] / clickProbs[True][N - 1]

	@staticmethod
	def getGamma(gammas, exams, k, prevClick, layout, intent):
		index = (2 if layout[k] else 0) + (1 if intent else 0)
		return gammas[index][k][k - prevClick - 1]# * rate + exams[k] * (1-rate)

	def _getClickProbs(self, s, possibleIntents):
		"""
			Returns clickProbs list
			clickProbs[i][k] = P(C_1, ..., C_k | I=i)
		"""
		clickProbs = dict((i, []) for i in possibleIntents)
		firstVerticalPos = -1 if not any(s.layout[:-1]) else [k for (k, l) in enumerate(s.layout) if l][0]
		prevClick = -1
		layout = [False] * len(s.layout) if self.ignoreLayout else s.layout
		for rank, c in enumerate(s.clicks):
			url = s.urls[rank]
			prob = {False: 0.0, True: 0.0}
			for i in possibleIntents:
				a = self.alpha[i][s.query][url]
				g = self.getGamma(self.gamma,s.exams, rank, prevClick, layout, i)
				#add mouse data
				#print('line 805 g = '+str(g))
				g = g * self.prate + s.exams[rank] * (1-self.prate)
				if self.explorationBias and any(s.layout[k] and s.clicks[k] for k in xrange(rank)) and not s.layout[rank]:
					g *= 1 - self.e[firstVerticalPos]
				prevProb = 1 if rank == 0 else clickProbs[i][-1]
				if c == 0:
					clickProbs[i].append(prevProb * (1 - a * g))
				else:
					clickProbs[i].append(prevProb * a * g)
			if c != 0:
				prevClick = rank
		return clickProbs

	def getRelevance(self, query_url_set, readInput):
		rel_set = {}
		count = 0
		for query in query_url_set:
			try:
				q_id = readInput.query_to_id[(query,readInput.region)]
				rel_set[query] = {}
				for url in query_url_set[query]:
					u_id = readInput.url_to_id[url]
					if self.alpha[False][q_id].has_key(u_id):
						rel_set[query][url] = self.alpha[False][q_id][u_id]
			except:
				continue
		#print('match ' + str(count) + ' ' + str(len(rel_set)))
		return rel_set
	
	def getRelSet(self):
		rel_set = {}
		for q in xrange(len(self.alpha[False])):
			rel_set[q] = {}
			for u in self.alpha[False][q]:
				rel_set[q][u] = self.alpha[False][q][u]
		return rel_set 

class MouseDcmModel(ClickModel):

	gammaTypesNum = 4
	
	def __init__(self, rate, ignoreIntents=True, ignoreLayout=True, explorationBias=False):
		self.explorationBias = explorationBias
		self.rate = rate
		print(str(self.rate))
		ClickModel.__init__(self, ignoreIntents, ignoreLayout)

	def train(self, sessions):
		possibleIntents = [False] if self.ignoreIntents else [False, True]
		urlRelFractions = dict((i, [defaultdict(lambda: [1.0, 1.0]) for q in xrange(MAX_QUERY_ID)]) for i in possibleIntents)
		gammaFractions = [[[1.0, 1.0] for g in xrange(self.gammaTypesNum)] for r in xrange(MAX_DOCS_PER_QUERY)]
		for s in sessions:
			query = s.query
			layout = [False] * len(s.layout) if self.ignoreLayout else s.layout
			lastClickedPos = MAX_DOCS_PER_QUERY - 1
			for k, c in enumerate(s.clicks):
				if c != 0:
					lastClickedPos = k
			intentWeights = {False: 1.0} if self.ignoreIntents else {False: 1 - s.intentWeight, True: s.intentWeight}
			#add mouse info
			mouseFractions = [[1.0 for g in xrange(self.gammaTypesNum)] for r in xrange(MAX_DOCS_PER_QUERY)]
			for k, (u, c) in enumerate(zip(s.urls, s.clicks[:(lastClickedPos + 1)])):
				for i in possibleIntents:
					if c != 0:
						urlRelFractions[i][query][u][1] += intentWeights[i]
						if k == lastClickedPos:
							self.getGamma(gammaFractions[k], k, layout, i)[1] += intentWeights[i]
						else:
							self.getGamma(gammaFractions[k], k, layout, i)[0] += intentWeights[i]
						#add mouse info
						if k < len(s.clicks)-1:
							index = 2 * (1 if layout[k + 1] else 0) + (1 if i else 0)
							mouseFractions[k][index] += intentWeights[i] * s.exams[k+1]
					else:
						urlRelFractions[i][query][u][0] += intentWeights[i]
		self.urlRelevances = dict((i, [defaultdict(lambda: DEFAULT_REL) for q in xrange(MAX_QUERY_ID)]) for i in possibleIntents)
		self.gammas = [[0.5 for g in xrange(self.gammaTypesNum)] for r in xrange(MAX_DOCS_PER_QUERY)]
		for i in possibleIntents:
			for query, d in enumerate(urlRelFractions[i]):
				if not d:
					continue
				for url, relFractions in d.iteritems():
					self.urlRelevances[i][query][url] = relFractions[1] / (relFractions[1] + relFractions[0])
		for k in xrange(MAX_DOCS_PER_QUERY):
			for g in xrange(self.gammaTypesNum):
				#self.gammas[k][g] = gammaFractions[k][g][0] / (gammaFractions[k][g][0] + gammaFractions[k][g][1])
				#add mouse info
				try:
					self.gammas[k][g] = (gammaFractions[k][g][0] - (1-self.rate)*mouseFractions[k][g]) / (gammaFractions[k][g][0] + gammaFractions[k][g][1]) / self.rate
				except:
					self.gammas[k][g] = 0
				#if self.gammas[k][g] < 0:
					#self.gammas[k][g] = 0

	def _getClickProbs(self, s, possibleIntents):
		clickProbs = {False: [], True: []}		  # P(C_1, ..., C_k)
		query = s.query
		layout = [False] * len(s.layout) if self.ignoreLayout else s.layout
		for i in possibleIntents:
			examinationProb = 1.0	   # P(C_1, ..., C_{k - 1}, E_k = 1)
			for k, c in enumerate(s.clicks):
				r = self.urlRelevances[i][query][s.urls[k]]
				prevProb = 1 if k == 0 else clickProbs[i][-1]
				if c == 0:
					clickProbs[i].append(prevProb - examinationProb * r)	# P(C_1, ..., C_k = 0) = P(C_1, ..., C_{k-1}) - P(C_1, ..., C_k = 1)
					examinationProb *= 1 - r								# P(C_1, ..., C_k, E_{k+1} = 1) = P(E_{k+1} = 1 | C_k, E_k = 1) * P(C_k | E_k = 1) *  P(C_1, ..., C_{k - 1}, E_k = 1)
				else:
					clickProbs[i].append(examinationProb * r)
					gamma = self.getGamma(self.gammas[k], k, layout, i) * self.rate
					if k < len(s.clicks) - 1:
						gamma += (1 - self.rate) * s.exams[k+1]
					#examinationProb *= self.getGamma(self.gammas[k], k, layout, i) * r  # P(C_1, ..., C_k, E_{k+1} = 1) = P(E_{k+1} = 1 | C_k, E_k = 1) * P(C_k | E_k = 1) *  P(C_1, ..., C_{k - 1}, E_k = 1)
					#add mouse info
					if gamma <= 0:
						gamma = 0.0001
					examinationProb *= gamma * r
		for i in possibleIntents:
			for j in range(0,len(clickProbs[i])):
				if clickProbs[i][j] <= 0:
					clickProbs[i][j] = 0.00000000000000000000001
		return clickProbs

	@staticmethod
	def getGamma(gammas, k, layout, intent):
		return DbnModel.getGamma(gammas, k, layout, intent)
	
	def getRelevance(self, query_url_set, readInput):
		rel_set = {}
		count = 0
		for query in query_url_set:
			try:
				q_id = readInput.query_to_id[(query,readInput.region)]
				rel_set[query] = {}
				for url in query_url_set[query]:
					u_id = readInput.url_to_id[url]
					if self.urlRelevances[False][q_id].has_key(u_id):
						rel_set[query][url] = self.urlRelevances[False][q_id][u_id]
			except:
				continue
		#print('match ' + str(count) + ' ' + str(len(rel_set)))
		return rel_set

class HuangDbnModel(ClickModel):

	def __init__(self, gammas, ignoreIntents=True, ignoreLayout=True):
		self.gammas = gammas
		print('Huang')
		ClickModel.__init__(self, ignoreIntents, ignoreLayout)

	def train(self, sessions):
		possibleIntents = [False] if self.ignoreIntents else [False, True]
		# intent -> query -> url -> (a_u, s_u)
		self.urlRelevances = dict((i, [defaultdict(lambda: {'a': DEFAULT_REL, 's': DEFAULT_REL}) for q in xrange(MAX_QUERY_ID)]) for i in possibleIntents)
		# here we store distribution of posterior intent weights given train data
		self.queryIntentsWeights = defaultdict(lambda: [])

		# EM algorithm
		if not PRETTY_LOG:
			print >>sys.stderr, '-' * 80
			print >>sys.stderr, 'Start. Current time is', datetime.now()
		for iteration_count in xrange(MAX_ITERATIONS):
			# urlRelFractions[intent][query][url][r][1] --- coefficient before \log r
			# urlRelFractions[intent][query][url][r][0] --- coefficient before \log (1 - r)
			urlRelFractions = dict((i, [defaultdict(lambda: {'a': [1.0, 1.0], 's': [1.0, 1.0]}) for q in xrange(MAX_QUERY_ID)]) for i in [False, True])	# set to store the parameters of Aquk, Squk
			self.queryIntentsWeights = defaultdict(lambda: [])
			# E step
			for s in sessions:
				#print(s.exams)
				positionRelevances = {} # set to store the parameters of Ak, Sk
				query = s.query
				for intent in possibleIntents:
					positionRelevances[intent] = {}
					for r in ['a', 's']:
						positionRelevances[intent][r] = [self.urlRelevances[intent][query][url][r] for url in s.urls]
				layout = [False] * len(s.layout) if self.ignoreLayout else s.layout
				sessionEstimate = dict((intent, self._getSessionEstimate(positionRelevances[intent], layout, s.clicks, s.exams, intent)) for intent in possibleIntents)

				# P(I | C, G)
				if self.ignoreIntents:
					p_I__C_G = {False: 1, True: 0}
				else:
					a = sessionEstimate[False]['C'] * (1 - s.intentWeight)
					b = sessionEstimate[True]['C'] * s.intentWeight
					p_I__C_G = {False: a / (a + b), True: b / (a + b)}
				self.queryIntentsWeights[query].append(p_I__C_G[True])
				for k, url in enumerate(s.urls):
					for intent in possibleIntents:
						# update a
						urlRelFractions[intent][query][url]['a'][1] += sessionEstimate[intent]['a'][k] * p_I__C_G[intent] #the estimate times when 'url' is estimated as A_k = 1
						urlRelFractions[intent][query][url]['a'][0] += (1 - sessionEstimate[intent]['a'][k]) * p_I__C_G[intent]	#the estimate times when 'url' is estimated as A_k = 0
						if s.clicks[k] != 0:
							# Update s
							urlRelFractions[intent][query][url]['s'][1] += sessionEstimate[intent]['s'][k] * p_I__C_G[intent]	#the estimate times when 'url' is estimated as S_k = 1
							urlRelFractions[intent][query][url]['s'][0] += (1 - sessionEstimate[intent]['s'][k]) * p_I__C_G[intent]	#the estimate times when 'url' is estimated as S_k = 0
			if not PRETTY_LOG:
				sys.stderr.write('E')

			# M step
			# update parameters and record mean square error
			sum_square_displacement = 0.0
			Q_functional = 0.0
			num_points = 0
			for i in possibleIntents:
				for query, d in enumerate(urlRelFractions[i]):
					if not d:
						continue
					for url, relFractions in d.iteritems():
						a_u_new = relFractions['a'][1] / (relFractions['a'][1] + relFractions['a'][0]) #recalculate: P(A_K=1) = P(A_K=1)/(P(A_K=1)+P(A_K=0))
						sum_square_displacement += (a_u_new - self.urlRelevances[i][query][url]['a']) ** 2
						num_points += 1
						self.urlRelevances[i][query][url]['a'] = a_u_new
						Q_functional += relFractions['a'][1] * math.log(a_u_new) + relFractions['a'][0] * math.log(1 - a_u_new)
						s_u_new = relFractions['s'][1] / (relFractions['s'][1] + relFractions['s'][0])	#recalculate: P(S_K=1) = P(S_K=1)/(P(S_K=1)+P(S_K=0))
						sum_square_displacement += (s_u_new - self.urlRelevances[i][query][url]['s']) ** 2
						num_points += 1
						self.urlRelevances[i][query][url]['s'] = s_u_new
						Q_functional += relFractions['s'][1] * math.log(s_u_new) + relFractions['s'][0] * math.log(1 - s_u_new)
			if not PRETTY_LOG:
				sys.stderr.write('M\n')
			rmsd = math.sqrt(sum_square_displacement / (num_points if TRAIN_FOR_METRIC else 1.0))
			if PRETTY_LOG:
				sys.stderr.write('%d..' % (iteration_count + 1))
			else:
				print >>sys.stderr, 'Iteration: %d, RMSD: %.10f' % (iteration_count + 1, rmsd)
				print >>sys.stderr, 'Q functional: %f' % Q_functional
		if PRETTY_LOG:
			sys.stderr.write('\n')
		for q, intentWeights in self.queryIntentsWeights.iteritems():
			self.queryIntentsWeights[q] = sum(intentWeights) / len(intentWeights)

	@staticmethod
	def testBackwardForward():
		positionRelevances = {'a': [0.5] * MAX_DOCS_PER_QUERY, 's': [0.5] * MAX_DOCS_PER_QUERY}
		gammas = [0.9] * 4
		layout = [False] * (MAX_DOCS_PER_QUERY + 1)
		clicks = [0] * MAX_DOCS_PER_QUERY
		alpha, beta = MouseDbnModel.getForwardBackwardEstimates(positionRelevances, gammas, layout, clicks, exams, False)
		x = alpha[0][0] * beta[0][0] + alpha[0][1] * beta[0][1]
		assert all(abs((a[0] * b[0] + a[1] * b[1]) / x  - 1) < 0.00001 for a, b in zip(alpha, beta))

	@staticmethod
	def getGamma(gammas, k, layout, intent):
		index = 2 * (1 if layout[k + 1] else 0) + (1 if intent else 0)
		return gammas[index]

	@staticmethod
	def getForwardBackwardEstimates(positionRelevances, gammas, layout, clicks, exams, intent):
		N = len(clicks)
		if DEBUG:
			assert N + 1 == len(layout)
		alpha = [[0.0, 0.0] for i in xrange(N + 1)]
		beta = [[0.0, 0.0] for i in xrange(N + 1)]
		alpha[0] = [0.0, 1.0]
		beta[N] = [1.0, 1.0]

		# P(E_{k+1} = e, C_k | E_k = e', G, I)
		updateMatrix = [[[0.0 for e1 in [0, 1]] for e in [0, 1]] for i in xrange(N)]
		for k, C_k in enumerate(clicks):
			a_u = positionRelevances['a'][k]
			s_u = positionRelevances['s'][k]
			gamma = MouseDbnModel.getGamma(gammas, k, layout, intent)
			#add mouse data
			if k+1 < N:
				if exams[k+1] > 0:
					gamma = 1.0	#add mouse predict exam
			if gamma == 0:
				gamma += 0.000000001
			if C_k == 0:
				updateMatrix[k][0][0] = 1							#P(ek+1=0,ck=0|ek=0)		
				updateMatrix[k][0][1] = (1 - gamma) * (1 - a_u)		#P(ek+1=0,ck=0|ek=1)
				updateMatrix[k][1][0] = 0							#P(ek+1=1,ck=0|ek=0)
				updateMatrix[k][1][1] = gamma * (1 - a_u)			#P(ek+1=1,ck=0|ek=1)
			else:
				updateMatrix[k][0][0] = 0							#P(ek+1=0,ck=1|ek=0)
				updateMatrix[k][0][1] = (s_u + (1 - gamma) * (1 - s_u)) * a_u	#P(ek+1=0,ck=1|ek=1)
				updateMatrix[k][1][0] = 0							#P(ek+1=1,ck=1|ek=0)
				updateMatrix[k][1][1] = gamma * (1 - s_u) * a_u		#P(ek+1=1,ck=1|ek=1)

		for k in xrange(N):
			for e in [0, 1]:	#we may add predict examine here?
				alpha[k + 1][e] = sum(alpha[k][e1] * updateMatrix[k][e][e1] for e1 in [0, 1])	#forward to P(e{k+1}=e)
				beta[N - 1 - k][e] = sum(beta[N - k][e1] * updateMatrix[N - 1 - k][e1][e] for e1 in [0, 1])	#backward to P(e{k+1}=e)

		return alpha, beta

	def _getSessionEstimate(self, positionRelevances, layout, clicks, exams, intent): #return the probability of occurence of this session
		# Returns {'a': P(A_k | I, C, G), 's': P(S_k | I, C, G), 'C': P(C | I, G), 'clicks': P(C_k | C_1, ..., C_{k-1}, I, G)} as a dict
		# sessionEstimate[True]['a'][k] = P(A_k = 1 | I = 'Fresh', C, G), probability of A_k = 0 can be calculated as 1 - p
		N = len(clicks)
		if DEBUG:
			assert N + 1 == len(layout)
		sessionEstimate = {'a': [0.0] * N, 's': [0.0] * N, 'e': [[0.0, 0.0] for k in xrange(N)], 'C': 0.0, 'clicks': [0.0] * N}

		alpha, beta = self.getForwardBackwardEstimates(positionRelevances, self.gammas, layout, clicks,exams, intent)
		try:
			#varphi[k] = (P(ek=0),P(ek=1))
			varphi = [((a[0] * b[0]) / (a[0] * b[0] + a[1] * b[1]), (a[1] * b[1]) / (a[0] * b[0] + a[1] * b[1])) for a, b in zip(alpha, beta)]
			'''
			for k, E_k in enumerate(exams):
				if k >= len(clicks):
					break
				varphi[k] = (self.rate * varphi[k][0] + (1 - self.rate) * (1 - E_k), self.rate * varphi[k][1] + (1 - self.rate) * (E_k))
			'''
		except ZeroDivisionError:
			print >>sys.stderr, alpha, beta, [(a[0] * b[0] + a[1] * b[1]) for a, b in zip(alpha, beta)], positionRelevances
			sys.exit(1)
		if DEBUG:
			assert all(ph[0] < 0.01 for ph, c in zip(varphi[:N], clicks) if c != 0), (alpha, beta, varphi, clicks)
		# calculate P(C | I, G) for k = 0
		sessionEstimate['C'] = alpha[0][0] * beta[0][0] + alpha[0][1] * beta[0][1]	  # == 0 + 1 * beta[0][1]
		#sessionEstimate['C'] = 1
		for k, C_k in enumerate(clicks):
			a_u = positionRelevances['a'][k]
			s_u = positionRelevances['s'][k]
			gamma = self.getGamma(self.gammas, k, layout, intent)
			# E_k_multiplier --- P(S_k = 0 | C_k) P(C_k | E_k = 1)
			if C_k == 0:
				sessionEstimate['a'][k] = a_u * varphi[k][0]
				sessionEstimate['s'][k] = 0.0
				#sessionEstimate['C'] = sessionEstimate['C'] * ((1 - a_u) * varphi[k][1] + varphi[k][0]) #new P(C | I, G)
			else:
				sessionEstimate['a'][k] = 1.0
				sessionEstimate['s'][k] = varphi[k + 1][0] * s_u / (s_u + (1 - gamma) * (1 - s_u))
				#sessionEstimate['C'] = sessionEstimate['C'] * (a_u * varphi[k][1]) #new P(C | I, G)
			# P(C_1, ..., C_k | I)  
			#this is the crucial parameter that is used to calculate LL & Perplexity
			sessionEstimate['clicks'][k] = sum(alpha[k + 1])
			'''
			if C_k != 0 and C_k != 1:
				print("ERROR")
			if k == 0:
				sessionEstimate['clicks'][k] = C_k * (varphi[k][1]*a_u) + (1-C_k)*(varphi[k][0] + varphi[k][1]*(1-a_u))
			else:
				sessionEstimate['clicks'][k] = sessionEstimate['clicks'][k-1] * (C_k * (varphi[k][1]*a_u) + (1-C_k)*(varphi[k][0] + varphi[k][1]*(1-a_u)))
			'''
		return sessionEstimate

	def _getClickProbs(self, s, possibleIntents):
		"""
			Returns clickProbs list:
			clickProbs[i][k] = P(C_1, ..., C_k | I=i)
		"""
		# TODO: ensure that s.clicks[l] not used to calculate clickProbs[i][k] for l >= k
		positionRelevances = {}
		for intent in possibleIntents:
			positionRelevances[intent] = {}
			for r in ['a', 's']:
				positionRelevances[intent][r] = [self.urlRelevances[intent][s.query][url][r] for url in s.urls]
				if QUERY_INDEPENDENT_PAGER:
					for k, u in enumerate(s.urls):
						if u == 'PAGER':
							# use dummy 0 query for all fake pager URLs
							positionRelevances[intent][r][k] = self.urlRelevances[intent][0][url][r]
		layout = [False] * len(s.layout) if self.ignoreLayout else s.layout
		return dict((i, self._getSessionEstimate(positionRelevances[i], layout, s.clicks, s.exams, i)['clicks']) for i in possibleIntents)
	
	def getRelevance(self, query_url_set, readInput):
		rel_set = {}
		count = 0
		for query in query_url_set:
			try:
				q_id = readInput.query_to_id[(query,readInput.region)]
				rel_set[query] = {}
				for url in query_url_set[query]:
					u_id = readInput.url_to_id[url]
					if self.urlRelevances[False][q_id].has_key(u_id):
						rel_set[query][url] = self.urlRelevances[False][q_id][u_id]['s']
			except:
				continue
		#print('match ' + str(count) + ' ' + str(len(rel_set)))
		return rel_set
	
	def getRelSet(self):
		rel_set = {}
		for q in xrange(len(self.urlRelevances[False])):
			rel_set[q] = {}
			for u in self.urlRelevances[False][q]:
				rel_set[q][u] = self.urlRelevances[False][q][u]['s']
		return rel_set 

class DMUbmModel(ClickModel):

	gammaTypesNum = 4

	def __init__(self, startRate, startStep, ignoreIntents=True, ignoreLayout=True, explorationBias=False):
		self.explorationBias = explorationBias
		self.rate = [startRate for p in xrange(MAX_DOCS_PER_QUERY)]
		self.preGradFlag = [1 for p in xrange(MAX_DOCS_PER_QUERY)]
		self.step = [startStep for p in xrange(MAX_DOCS_PER_QUERY)]
		print(str(startRate))
		ClickModel.__init__(self, ignoreIntents, ignoreLayout)

	def train(self, sessions):
		possibleIntents = [False] if self.ignoreIntents else [False, True]
		# alpha: intent -> query -> url -> "attractiveness probability"
		self.alpha = dict((i, [defaultdict(lambda: DEFAULT_REL) for q in xrange(MAX_QUERY_ID)]) for i in possibleIntents)
		# gamma: freshness of the current result: gammaType -> rank -> "distance from the last click" - 1 -> examination probability
		self.gamma = [[[0.5 for d in xrange(MAX_DOCS_PER_QUERY)] for r in xrange(MAX_DOCS_PER_QUERY)] for g in xrange(self.gammaTypesNum)]
		if self.explorationBias:
			self.e = [0.5 for p in xrange(MAX_DOCS_PER_QUERY)]
		if not PRETTY_LOG:
			print >>sys.stderr, '-' * 80
			print >>sys.stderr, 'Start. Current time is', datetime.now()
		for iteration_count in xrange(MAX_ITERATIONS):
			self.queryIntentsWeights = defaultdict(lambda: [])
			# not like in DBN! xxxFractions[0] is a numerator while xxxFraction[1] is a denominator
			alphaFractions = dict((i, [defaultdict(lambda: [1.0, 2.0]) for q in xrange(MAX_QUERY_ID)]) for i in possibleIntents)
			gammaFractions = [[[[1.0, 2.0] for d in xrange(MAX_DOCS_PER_QUERY)] for r in xrange(MAX_DOCS_PER_QUERY)] for g in xrange(self.gammaTypesNum)]
			if self.explorationBias:
				eFractions = [[1.0, 2.0] for p in xrange(MAX_DOCS_PER_QUERY)]
			# E-step
			for s in sessions:
				query = s.query
				layout = [False] * len(s.layout) if self.ignoreLayout else s.layout
				if self.explorationBias:
					explorationBiasPossible = any((l and c for (l, c) in zip(s.layout, s.clicks)))
					firstVerticalPos = -1 if not any(s.layout[:-1]) else [k for (k, l) in enumerate(s.layout) if l][0]
				if self.ignoreIntents:
					p_I__C_G = {False: 1.0, True: 0}
				else:
					a = self._getSessionProb(s) * (1 - s.intentWeight)
					b = 1 * s.intentWeight
					p_I__C_G = {False: a / (a + b), True: b / (a + b)}
				self.queryIntentsWeights[query].append(p_I__C_G[True])
				prevClick = -1
				for rank, c in enumerate(s.clicks):
					url = s.urls[rank]
					for intent in possibleIntents:
						a = self.alpha[intent][query][url]
						if self.explorationBias and explorationBiasPossible:
							e = self.e[firstVerticalPos]
						if c == 0:
							g = self.getGamma(self.gamma, s.exams, rank, prevClick, layout, intent)
							#add mouse data
							rate = self.rate[rank]
							g = g * rate + s.exams[rank] * (1-rate)
							gCorrection = 1
							if self.explorationBias and explorationBiasPossible and not s.layout[k]:
								gCorrection = 1 - e
								g *= gCorrection
							alphaFractions[intent][query][url][0] += a * (1 - g) / (1 - a * g) * p_I__C_G[intent]
							self.getGamma(gammaFractions, s.exams, rank, prevClick, layout, intent)[0] += g / gCorrection * (1 - a) / (1 - a * g) * p_I__C_G[intent]
							if self.explorationBias and explorationBiasPossible:
								eFractions[firstVerticalPos][0] += (e if s.layout[k] else e / (1 - a * g)) * p_I__C_G[intent]
						else:
							alphaFractions[intent][query][url][0] += 1 * p_I__C_G[intent]
							self.getGamma(gammaFractions, s.exams, rank, prevClick, layout, intent)[0] += 1 * p_I__C_G[intent]
							if self.explorationBias and explorationBiasPossible:
								eFractions[firstVerticalPos][0] += (e if s.layout[k] else 0) * p_I__C_G[intent]
						alphaFractions[intent][query][url][1] += 1 * p_I__C_G[intent]
						self.getGamma(gammaFractions, s.exams, rank, prevClick, layout, intent)[1] += 1 * p_I__C_G[intent]
						if self.explorationBias and explorationBiasPossible:
							eFractions[firstVerticalPos][1] += 1 * p_I__C_G[intent]
					if c != 0:
						prevClick = rank
			if not PRETTY_LOG:
				sys.stderr.write('E')
			# M-step
			sum_square_displacement = 0.0
			num_points = 0
			for i in possibleIntents:
				for q in xrange(MAX_QUERY_ID):
					for url, aF in alphaFractions[i][q].iteritems():
						new_alpha = aF[0] / aF[1]
						sum_square_displacement += (self.alpha[i][q][url] - new_alpha) ** 2
						num_points += 1
						self.alpha[i][q][url] = new_alpha
			for g in xrange(self.gammaTypesNum):
				for r in xrange(MAX_DOCS_PER_QUERY):
					for d in xrange(MAX_DOCS_PER_QUERY):
						gF = gammaFractions[g][r][d]
						new_gamma = gF[0] / gF[1]
						sum_square_displacement += (self.gamma[g][r][d] - new_gamma) ** 2
						num_points += 1
						self.gamma[g][r][d] = new_gamma
			if self.explorationBias:
				for p in xrange(MAX_DOCS_PER_QUERY):
					new_e = eFractions[p][0] / eFractions[p][1]
					sum_square_displacement += (self.e[p] - new_e) ** 2
					num_points += 1
					self.e[p] = new_e
			if not PRETTY_LOG:
				sys.stderr.write('M\n')
			rmsd = math.sqrt(sum_square_displacement / (num_points if TRAIN_FOR_METRIC else 1.0))
			if PRETTY_LOG:
				sys.stderr.write('%d..' % (iteration_count + 1))
			else:
				print >>sys.stderr, 'Iteration: %d, RMSD: %.10f' % (iteration_count + 1, rmsd)
				
			# Gradient Descent for rate
			self._updateRate(sessions)
			
		if PRETTY_LOG:
			sys.stderr.write('\n')
		for q, intentWeights in self.queryIntentsWeights.iteritems():
			self.queryIntentsWeights[q] = sum(intentWeights) / len(intentWeights)

	def _getSessionProb(self, s):
		clickProbs = self._getClickProbs(s, [False, True])
		N = len(s.clicks)
		return clickProbs[False][N - 1] / clickProbs[True][N - 1]
	
	def _updateRate(self, sessions):
		grad = [0.0 for p in xrange(MAX_DOCS_PER_QUERY)]
		possibleIntents = [False] if self.ignoreIntents else [False, True]
		for s in sessions:
			clickProbs = dict((i, []) for i in possibleIntents)
			firstVerticalPos = -1 if not any(s.layout[:-1]) else [k for (k, l) in enumerate(s.layout) if l][0]
			prevClick = -1
			layout = [False] * len(s.layout) if self.ignoreLayout else s.layout
			for rank, c in enumerate(s.clicks):
				url = s.urls[rank]
				prob = {False: 0.0, True: 0.0}
				for i in possibleIntents:
					a = self.alpha[i][s.query][url]
					g = self.getGamma(self.gamma,s.exams, rank, prevClick, layout, i)
					rate = self.rate[rank]
					#add mouse data
					Er = g * rate + s.exams[rank] * (1-rate)
					if self.explorationBias and any(s.layout[k] and s.clicks[k] for k in xrange(rank)) and not s.layout[rank]:
						Er *= 1 - self.e[firstVerticalPos]
					prevProb = 1 if rank == 0 else clickProbs[i][-1]
					if c == 0:
						clickProbs[i].append(prevProb * (1 - a * Er))
					else:
						clickProbs[i].append(prevProb * a * Er)
					#update gradient
					if c == 0:
						grad[rank] += -a / (1 - a * Er) * (g - s.exams[rank]) 
					else:
						grad[rank] += 1 / Er * (g - s.exams[rank])
				if c != 0:
					prevClick = rank
		grad = [grad[p] / len(sessions) for p in xrange(MAX_DOCS_PER_QUERY)]
		#change step length
		for p in xrange(MAX_DOCS_PER_QUERY):
			if grad[p] * self.preGradFlag[p] > 0:
				self.step[p] = self.step[p] * 2.0
			else:
				self.step[p] = self.step[p] / 2.0
			self.preGradFlag[p] = 1.0 if grad[p] > 0.0 else -1.0
		for p in xrange(MAX_DOCS_PER_QUERY):
			self.rate[p] += grad[p] * self.step[p]
			if self.rate[p] > 1.0 :
				self.rate[p] = 1.0
			if self.rate[p] < 0.0:
				self.rate[p] = 0.0
		print(self.rate)
		
	@staticmethod
	def getGamma(gammas, exams, k, prevClick, layout, intent):
		index = (2 if layout[k] else 0) + (1 if intent else 0)
		return gammas[index][k][k - prevClick - 1]# * rate + exams[k] * (1-rate)

	def _getClickProbs(self, s, possibleIntents):
		"""
			Returns clickProbs list
			clickProbs[i][k] = P(C_1, ..., C_k | I=i)
		"""
		clickProbs = dict((i, []) for i in possibleIntents)
		firstVerticalPos = -1 if not any(s.layout[:-1]) else [k for (k, l) in enumerate(s.layout) if l][0]
		prevClick = -1
		layout = [False] * len(s.layout) if self.ignoreLayout else s.layout
		for rank, c in enumerate(s.clicks):
			url = s.urls[rank]
			prob = {False: 0.0, True: 0.0}
			for i in possibleIntents:
				a = self.alpha[i][s.query][url]
				g = self.getGamma(self.gamma,s.exams, rank, prevClick, layout, i)
				#add mouse data
				rate = self.rate[rank]
				g = g * rate + s.exams[rank] * (1-rate)
				if self.explorationBias and any(s.layout[k] and s.clicks[k] for k in xrange(rank)) and not s.layout[rank]:
					g *= 1 - self.e[firstVerticalPos]
				prevProb = 1 if rank == 0 else clickProbs[i][-1]
				if c == 0:
					clickProbs[i].append(prevProb * (1 - a * g))
				else:
					clickProbs[i].append(prevProb * a * g)
			if c != 0:
				prevClick = rank
		return clickProbs

	def getRelevance(self, query_url_set, readInput):
		rel_set = {}
		count = 0
		for query in query_url_set:
			try:
				q_id = readInput.query_to_id[(query,readInput.region)]
				rel_set[query] = {}
				for url in query_url_set[query]:
					u_id = readInput.url_to_id[url]
					if self.alpha[False][q_id].has_key(u_id):
						rel_set[query][url] = self.alpha[False][q_id][u_id]
			except:
				continue
		#print('match ' + str(count) + ' ' + str(len(rel_set)))
		return rel_set
	
	def getRelSet(self):
		rel_set = {}
		for q in xrange(len(self.alpha[False])):
			rel_set[q] = {}
			for u in self.alpha[False][q]:
				rel_set[q][u] = self.alpha[False][q][u]
		return rel_set 


class InputReader:
	def __init__(self, discardNoClicks):
		self.url_to_id = {}
		self.query_to_id = {}
		self.current_url_id = 1
		self.current_query_id = 0
		self.discardNoClicks = discardNoClicks

	def __call__(self, f):
		sessions = []
		for line in f:
			#line = line.decode('gb2312')
			#line = line.encode('utf8')
			
			query, urls, clicks , exams = line.rstrip().split('\t')
			urls, clicks, exams = map(json.loads, [urls, clicks, exams])
			'''
			#filter click = 1 and exams = 0 
			for rank, c in enumerate(clicks):
				if c != 0 and exams[rank] == 0:
					continue
			'''
			layout = [False for i in xrange(len(clicks))] 
			intentWeight = 0
			self.region = 50
			region = 50
			
			#hash_digest, query, region, intentWeight, urls, layout, clicks , exams = line.rstrip().split('\t')
			#print(exams)
			#try:
			#urls, layout, clicks, exams = map(json.loads, [urls, layout, clicks, exams])
			#except:
			
			#	continue
			extra = {}
			urlsObserved = 0
			if EXTENDED_LOG_FORMAT:
				maxLen = MAX_DOCS_PER_QUERY
				if TRANSFORM_LOG:
					maxLen -= MAX_DOCS_PER_QUERY // SERP_SIZE
				urls, _ = self.convertToList(urls, '', maxLen)
				for u in urls:
					if u == '':
						break
					urlsObserved += 1
				urls = urls[:urlsObserved]
				layout, _ = self.convertToList(layout, False, urlsObserved)
				clicks, extra = self.convertToList(clicks, 0, urlsObserved)
			else:
				urls = urls[:MAX_DOCS_PER_QUERY]
				urlsObserved = len(urls)
				layout = layout[:urlsObserved]
				clicks = clicks[:urlsObserved]
			if urlsObserved < MIN_DOCS_PER_QUERY:
				continue
			if self.discardNoClicks and not any(clicks):
				continue
			if float(intentWeight) > 1 or float(intentWeight) < 0:
				continue
			if (query, region) in self.query_to_id:
				query_id = self.query_to_id[(query, region)]
			else:
				query_id = self.current_query_id
				self.query_to_id[(query, region)] = self.current_query_id
				self.current_query_id += 1
			intentWeight = float(intentWeight)
			# add fake G_{MAX_DOCS_PER_QUERY+1} to simplify gamma calculation:
			layout.append(False)
			url_ids = []
			for u in urls:
				if u in ['_404', 'STUPID', 'VIRUS', 'SPAM']:
					# convert Yandex-specific fields to standard ones
					assert TRAIN_FOR_METRIC
					u = 'IRRELEVANT'
				if u.startswith('RELEVANT_'):
					# convert Yandex-specific fields to standard ones
					assert TRAIN_FOR_METRIC
					u = 'RELEVANT'
				if u in self.url_to_id:
					if TRAIN_FOR_METRIC:
						url_ids.append(u)
					else:
						url_ids.append(self.url_to_id[u])
				else:
					urlid = self.current_url_id
					if TRAIN_FOR_METRIC:
						url_ids.append(u)
					else:
						url_ids.append(urlid)
					self.url_to_id[u] = urlid
					self.current_url_id += 1
			sessions.append(SessionItem(intentWeight, query_id, url_ids, layout, clicks, extra , exams))
		# FIXME: bad style
		global MAX_QUERY_ID
		MAX_QUERY_ID = self.current_query_id + 1
		return sessions

	@staticmethod
	def convertToList(sparseDict, defaultElem=0, maxLen=MAX_DOCS_PER_QUERY):
		""" Convert dict of the format {"0": doc0, "13": doc13} to the list of the length MAX_DOCS_PER_QUERY """
		convertedList = [defaultElem] * maxLen
		extra = {}
		for k, v in sparseDict.iteritems():
			try:
				convertedList[int(k)] = v
			except (ValueError, IndexError):
				extra[k] = v
		return convertedList, extra

	@staticmethod
	def mergeExtraToSessionItem(s):
		""" Put pager click into the session item (presented as a fake URL) """
		if s.extraclicks.get('TRANSFORMED', False):
			return s
		else:
			newUrls = []
			newLayout = []
			newClicks = []
			a = 0
			while a + SERP_SIZE <= len(s.urls):
				b = a + SERP_SIZE
				newUrls += s.urls[a:b]
				newLayout += s.layout[a:b]
				newClicks += s.clicks[a:b]
				# TODO: try different fake urls for different result pages (page_1, page_2, etc.)
				newUrls.append('PAGER')
				newLayout.append(False)
				newClicks.append(1)
				a = b
			newClicks[-1] = 0 if a == len(s.urls) else 1
			newLayout.append(False)
			if DEBUG:
				assert len(newUrls) == len(newClicks)
				assert len(newUrls) + 1 == len(newLayout), (len(newUrls), len(newLayout))
				assert len(newUrls) < len(s.urls) + MAX_DOCS_PER_QUERY / SERP_SIZE, (len(s.urls), len(newUrls))
			return SessionItem(s.intentWeight, s.query, newUrls, newLayout, newClicks, {'TRANSFORMED': True} , s.exams)

	
		


if __name__ == '__main__':
	if DEBUG:
		DbnModel.testBackwardForward()
	allCombinations = []
	interestingValues = [0.9, 1.0]
	for g1 in interestingValues:
		for g2 in interestingValues:
			for g3 in interestingValues:
				for g4 in interestingValues:
					allCombinations.append((g1, g2, g3, g4))

	readInput = InputReader()
	sessions = readInput(sys.stdin)

	if TRAIN_FOR_METRIC and PRINT_EBU_STATS:
		# ---------------------------------------------------------------
		#						   For EBU
		# ---------------------------------------------------------------
		# Relevance -> P(Click | Relevance)
		p_C_R_frac = defaultdict(lambda: [0, 0.0001])
		# Relevance -> P(Leave | Click, Relevance)
		p_L_C_R_frac = defaultdict(lambda: [0, 0.0001])
		for s in sessions:
			lastClickPos = max((i for i, c in enumerate(s.clicks) if c != 0))
			for i in xrange(lastClickPos + 1):
				u = s.urls[i]
				if s.clicks[i] != 0:
					p_C_R_frac[u][0] += 1
					if i == lastClickPos:
						p_L_C_R_frac[u][0] += 1
					p_L_C_R_frac[u][1] += 1
				p_C_R_frac[u][1] += 1

		for u in ['IRRELEVANT', 'RELEVANT', 'USEFUL', 'VITAL']:
			print 'P(C|%s)\t%f\tP(L|C,%s)\t%f' % (u, float(p_C_R_frac[u][0]) / p_C_R_frac[u][1], u, float(p_L_C_R_frac[u][0]) / p_L_C_R_frac[u][1])
		# ---------------------------------------------------------------

	if len(sys.argv) > 1:
		with open(sys.argv[1]) as test_clicks_file:
			testSessions = readInput(test_clicks_file)
	else:
		testSessions = sessions
	del readInput	   # needed to minimize memory consumption (see gc.collect() below)

	if TRANSFORM_LOG:
		assert EXTENDED_LOG_FORMAT
		sessions, testSessions = ([x for x in (InputReader.mergeExtraToSessionItem(s) for s in ss) if x] for ss in [sessions, testSessions])
	else:
		sessions, testSessions = ([s for s in ss if InputReader.mergeExtraToSessionItem(s)] for ss in [sessions, testSessions])

	print 'Train sessions: %d, test sessions: %d' % (len(sessions), len(testSessions))
	print 'Number of train sessions with 10+ urls shown:', len([s for s in sessions if len(s.urls) > SERP_SIZE + 1])
#	clickProbs = [0.0] * MAX_DOCS_PER_QUERY
	#counts = [0] * MAX_DOCS_PER_QUERY
	#for s in sessions:
		#for i, c in enumerate(s.clicks):
			#clickProbs[i] += 1 if c else 0
			#counts[i] += 1
	#print '\t'.join((str(x / cnt if cnt else x) for (x, cnt) in zip(clickProbs, counts)))
#	sys.exit(0)

	if 'Baseline' in USED_MODELS:
		baselineModel = ClickModel()
		baselineModel.train(sessions)
		print 'Baseline:', baselineModel.test(testSessions)

	if 'SDBN' in USED_MODELS:
		sdbnModel = SimplifiedDbnModel()
		sdbnModel.train(sessions)
		if TRANSFORM_LOG:
			print '(a_p, s_p) = ', sdbnModel.urlRelevances[False][0]['PAGER']
		print 'SDBN:', sdbnModel.test(testSessions)
		del sdbnModel		# needed to minimize memory consumption (see gc.collect() below)

	if 'UBM' in USED_MODELS:
		ubmModel = UbmModel()
		ubmModel.train(sessions)
		if TRAIN_FOR_METRIC:
			print '\n'.join(['%s\t%f' % r for r in \
					[(x, ubmModel.alpha[False][0][x]) for x in \
							['IRRELEVANT', 'RELEVANT', 'USEFUL', 'VITAL']]])
			for d in xrange(MAX_DOCS_PER_QUERY):
				for r in xrange(MAX_DOCS_PER_QUERY):
					print ('%.4f ' % (ubmModel.gamma[0][r][MAX_DOCS_PER_QUERY - 1 - d] if r + d >= MAX_DOCS_PER_QUERY - 1 else 0)),
				print
		print 'UBM', ubmModel.test(testSessions)
		del ubmModel	   # needed to minimize memory consumption (see gc.collect() below)

	if 'UBM-IA' in USED_MODELS:
		ubmModel = UbmModel(ignoreIntents=False, ignoreLayout=False)
		ubmModel.train(sessions)
		print 'UBM-IA', ubmModel.test(testSessions)
		del ubmModel	   # needed to minimize memory consumption (see gc.collect() below)

	if 'EB_UBM' in USED_MODELS:
		ebUbmModel = EbUbmModel()
		ebUbmModel.train(sessions)
		# print 'Exploration bias:', ebUbmModel.e
		print 'EB_UBM', ebUbmModel.test(testSessions)
		del ebUbmModel	   # needed to minimize memory consumption (see gc.collect() below)

	if 'EB_UBM-IA' in USED_MODELS:
		ebUbmModel = EbUbmModel(ignoreIntents=False, ignoreLayout=False)
		ebUbmModel.train(sessions)
		# print 'Exploration bias:', ebUbmModel.e
		print 'EB_UBM-IA', ebUbmModel.test(testSessions)
		del ebUbmModel	   # needed to minimize memory consumption (see gc.collect() below)

	if 'DCM' in USED_MODELS:
		dcmModel = DcmModel()
		dcmModel.train(sessions)
		if TRAIN_FOR_METRIC:
			print '\n'.join(['%s\t%f' % r for r in \
				[(x, dcmModel.urlRelevances[False][0][x]) for x in \
						['IRRELEVANT', 'RELEVANT', 'USEFUL', 'VITAL']]])
			print 'DCM gammas:', dcmModel.gammas
		print 'DCM', dcmModel.test(testSessions)
		del dcmModel	   # needed to minimize memory consumption (see gc.collect() below)

	if 'DCM-IA' in USED_MODELS:
		dcmModel = DcmModel(ignoreIntents=False, ignoreLayout=False)
		dcmModel.train(sessions)
		# print 'DCM gammas:', dcmModel.gammas
		print 'DCM-IA', dcmModel.test(testSessions)
		del dcmModel	   # needed to minimize memory consumption (see gc.collect() below)

	if 'DBN' in USED_MODELS:
		dbnModel = DbnModel((0.9, 0.9, 0.9, 0.9))
		dbnModel.train(sessions)
		print 'DBN:', dbnModel.test(testSessions)
		del dbnModel	   # needed to minimize memory consumption (see gc.collect() below)

	if 'DBN-IA' in USED_MODELS:
		for gammas in allCombinations:
			gc.collect()
			dbnModel = DbnModel(gammas, ignoreIntents=False, ignoreLayout=False)
			dbnModel.train(sessions)
			print 'DBN-IA: %.2f %.2f %.2f %.2f' % gammas, dbnModel.test(testSessions)

