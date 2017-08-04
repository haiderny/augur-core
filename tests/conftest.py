from binascii import hexlify
from datetime import timedelta
from ethereum.tools import tester
from ethereum.abi import ContractTranslator
from ethereum.tools.tester import ABIContract
from ethereum.config import config_metropolis, Env
from io import open as io_open
from json import dump as json_dump, load as json_load
from os import path, walk, makedirs, listdir
from pytest import fixture
from re import findall
from serpent import mk_full_signature, compile as compile_serpent
from solc import compile_standard
from utils import bytesToHexString, bytesToLong
from copy import deepcopy

# used to resolve relative paths
BASE_PATH = path.dirname(path.abspath(__file__))
def resolveRelativePath(relativeFilePath):
    return path.abspath(path.join(BASE_PATH, relativeFilePath))
COMPILATION_CACHE = resolveRelativePath('./compilation_cache')

class ContractsFixture:
    signatures = {}
    compiledCode = {}

    ####
    #### Static Methods
    ####

    @staticmethod
    def ensureCacheDirectoryExists():
        if not path.exists(COMPILATION_CACHE):
            makedirs(COMPILATION_CACHE)

    @staticmethod
    def generateSignature(relativeFilePath):
        ContractsFixture.ensureCacheDirectoryExists()
        filename = path.basename(relativeFilePath)
        name = path.splitext(filename)[0]
        outputPath = path.join(COMPILATION_CACHE,  name + 'Signature')
        lastCompilationTime = path.getmtime(outputPath) if path.isfile(outputPath) else 0
        if path.getmtime(relativeFilePath) > lastCompilationTime:
            print('generating signature for ' + name)
            extension = path.splitext(filename)[1]
            signature = None
            if extension == '.se':
                signature = mk_full_signature(relativeFilePath)
            elif extension == '.sol':
                signature = ContractsFixture.compileSolidity(relativeFilePath)['abi']
            else:
                raise
            with open(outputPath, mode='w') as file:
                json_dump(signature, file)
        else:
            print('using cached signature for ' + name)
        with open(outputPath, 'r') as file:
            signature = json_load(file)
        return(signature)

    @staticmethod
    def getCompiledCode(relativeFilePath):
        filename = path.basename(relativeFilePath)
        name = path.splitext(filename)[0]
        if name in ContractsFixture.compiledCode:
            return ContractsFixture.compiledCode[name]
        dependencySet = set()
        ContractsFixture.getAllDependencies(relativeFilePath, dependencySet)
        ContractsFixture.ensureCacheDirectoryExists()
        compiledOutputPath = path.join(COMPILATION_CACHE, name)
        lastCompilationTime = path.getmtime(compiledOutputPath) if path.isfile(compiledOutputPath) else 0
        needsRecompile = False
        for dependencyPath in dependencySet:
            if (path.getmtime(dependencyPath) > lastCompilationTime):
                needsRecompile = True
                break
        if (needsRecompile):
            print('compiling ' + name + '...')
            extension = path.splitext(filename)[1]
            compiledCode = None
            if extension == '.se':
                compiledCode = compile_serpent(relativeFilePath)
            elif extension == '.sol':
                compiledCode = bytearray.fromhex(ContractsFixture.compileSolidity(relativeFilePath)['evm']['bytecode']['object'])
            else:
                raise
            with io_open(compiledOutputPath, mode='wb') as file:
                file.write(compiledCode)
        else:
            print('using cached compilation for ' + name)
        with io_open(compiledOutputPath, mode='rb') as file:
            compiledCode = file.read()
            ContractsFixture.compiledCode[name] = compiledCode
            return(compiledCode)

    @staticmethod
    def compileSolidity(relativeFilePath):
        absoluteFilePath = resolveRelativePath(relativeFilePath)
        filename = path.basename(relativeFilePath)
        contractName = path.splitext(filename)[0]
        compilerParameter = {
            'language': 'Solidity',
            'sources': {
                filename: {
                    'urls': [ absoluteFilePath ]
                }
            },
            'settings': {
                'remappings': [ 'ROOT=%s' % resolveRelativePath("../src") ]
            },
            'outputSelection': {
                '*': [ 'metadata', 'evm.bytecode', 'evm.sourceMap' ]
            }
        }
        return compile_standard(compilerParameter, allow_paths=resolveRelativePath("../"))['contracts'][filename][contractName]

    @staticmethod
    def getAllDependencies(filePath, knownDependencies):
        knownDependencies.add(filePath)
        fileDirectory = path.dirname(filePath)
        with open(filePath, 'r') as file:
            fileContents = file.read()
        matches = findall("inset\('(.*?)'\)", fileContents)
        for match in matches:
            dependencyPath = path.abspath(path.join(fileDirectory, match))
            if not dependencyPath in knownDependencies:
                ContractsFixture.getAllDependencies(dependencyPath, knownDependencies)
        matches = findall("create\('(.*?)'\)", fileContents)
        for match in matches:
            dependencyPath = path.abspath(path.join(fileDirectory, match))
            if not dependencyPath in knownDependencies:
                ContractsFixture.getAllDependencies(dependencyPath, knownDependencies)
        matches = findall("import ['\"]ROOT/(.*?)['\"]", fileContents)
        for match in matches:
            dependencyPath = path.join(BASE_PATH, '..', 'src', match)
            if not dependencyPath in knownDependencies:
                ContractsFixture.getAllDependencies(dependencyPath, knownDependencies)
        return(knownDependencies)

    ####
    #### Class Methods
    ####

    def __init__(self):
        tester.GASPRICE = 0
        config_metropolis['BLOCK_GAS_LIMIT'] = 2**60
        self.chain = tester.Chain(env=Env(config=config_metropolis))
        self.contracts = {}
        self.controller = self.upload('../src/Controller.sol')
        assert self.controller.owner() == bytesToHexString(tester.a0)
        self.uploadAllContracts()
        self.whitelistTradingContracts()
        self.initializeAllContracts()
        self.branch = self.createBranch(0, 0)
        self.cash = self.getSeededCash()
        self.binaryMarket = self.createReasonableBinaryMarket(self.branch, self.cash)
        self.categoricalMarket = self.createReasonableCategoricalMarket(self.branch, 3, self.cash)
        self.scalarMarket = self.createReasonableScalarMarket(self.branch, -10 * 10**18, 30 * 10**18, self.cash)
        self.chain.mine(1)
        self.originalHead = self.chain.head_state
        self.originalBlock = self.chain.block
        self.originalContracts = deepcopy(self.contracts)
        self.snapshot = self.chain.snapshot()

    def uploadAndAddToController(self, relativeFilePath, lookupKey = None, signatureKey = None, constructorArgs=[]):
        lookupKey = lookupKey if lookupKey else path.splitext(path.basename(relativeFilePath))[0]
        contract = self.upload(relativeFilePath, lookupKey, signatureKey, constructorArgs)
        self.controller.setValue(lookupKey.ljust(32, '\x00'), contract.address)
        return(contract)

    def upload(self, relativeFilePath, lookupKey = None, signatureKey = None, constructorArgs=[]):
        resolvedPath = resolveRelativePath(relativeFilePath)
        lookupKey = lookupKey if lookupKey else path.splitext(path.basename(resolvedPath))[0]
        signatureKey = signatureKey if signatureKey else lookupKey
        if lookupKey in self.contracts:
            return(self.contracts[lookupKey])
        compiledCode = ContractsFixture.getCompiledCode(resolvedPath)
        if signatureKey not in ContractsFixture.signatures:
            ContractsFixture.signatures[signatureKey] = ContractsFixture.generateSignature(resolvedPath)
        signature = ContractsFixture.signatures[signatureKey]
        contractTranslator = ContractTranslator(signature)
        if len(constructorArgs) > 0:
            compiledCode += contractTranslator.encode_constructor_arguments(constructorArgs)
        contractAddress = bytesToLong(self.chain.contract(compiledCode, startgas=long(6.7 * 10**6)))
        contract = ABIContract(self.chain, contractTranslator, contractAddress)
        self.contracts[lookupKey] = contract
        return(contract)

    def applySignature(self, signatureName, address):
        assert address
        translator = ContractTranslator(ContractsFixture.signatures[signatureName])
        contract = ABIContract(self.chain, translator, address)
        return contract

    def resetSnapshot(self):
        self.chain.block = self.originalBlock
        self.chain.revert(self.snapshot)
        self.chain.head_state = self.originalHead
        self.contracts = deepcopy(self.originalContracts)

    ####
    #### Bulk Operations
    ####

    def uploadAllContracts(self):
        for directory, _, filenames in walk(resolveRelativePath('../src')):
            # skip the legacy reputation directory since it is unnecessary and we don't support uploads of contracts with constructors yet
            if 'legacy_reputation' in directory: continue
            for filename in filenames:
                name = path.splitext(filename)[0]
                extension = path.splitext(filename)[1]
                if extension != '.se' and extension != '.sol': continue
                if name == 'controller': continue
                contractsToDelegate = ['orders','tradingEscapeHatch']
                if name in contractsToDelegate:
                    delegationTargetName = "".join([name, "Target"])
                    self.uploadAndAddToController(path.join(directory, filename), delegationTargetName, name)
                    self.uploadAndAddToController("../src/libraries/Delegator.sol", name, "delegator", constructorArgs=[self.controller.address, delegationTargetName.ljust(32, '\x00')])
                    self.contracts[name] = self.applySignature(name, self.contracts[name].address)
                else:
                    self.uploadAndAddToController(path.join(directory, filename))

    def whitelistTradingContracts(self):
        for filename in listdir(resolveRelativePath('../src/trading')):
            name = path.splitext(filename)[0]
            extension = path.splitext(filename)[1]
            if extension != '.se': continue
            self.controller.addToWhitelist(self.contracts[name].address)

    def initializeAllContracts(self):
        contractsToInitialize = ['Mutex', 'Cash','orders','completeSets','makeOrder','takeBidOrder','takeAskOrder','takeOrder','cancelOrder','trade','claimProceeds','tradingEscapeHatch','ordersFetcher']
        for contractName in contractsToInitialize:
            self.contracts[contractName].initialize(self.controller.address)

    ####
    #### Helpers
    ####

    def getSeededCash(self):
        cash = self.contracts['Cash']
        cash.publicDepositEther(value = 1, sender = tester.k9)
        return cash

    def uploadShareToken(self, controllerAddress = None):
        controllerAddress = controllerAddress if controllerAddress else self.controller.address
        self.ensureShareTokenDependencies()
        shareTokenFactory = self.contracts['ShareTokenFactory']
        shareToken = shareTokenFactory.createShareToken(controllerAddress)
        return self.applySignature('shareToken', shareToken)

    def createBranch(self, parentBranch, payoutDistributionHash):
        branchAddress = self.contracts['BranchFactory'].createBranch(self.controller.address, parentBranch, payoutDistributionHash)
        branch = self.applySignature('branch', branchAddress)
        return branch

    def getReportingToken(self, market, payoutDistribution):
        reportingTokenAddress = market.getReportingToken(payoutDistribution)
        assert reportingTokenAddress
        reportingToken = ABIContract(self.chain, ContractTranslator(ContractsFixture.signatures['reportingToken']), reportingTokenAddress)
        return reportingToken

    def getChildBranch(self, parentBranch, market, payoutDistribution):
        payoutDistributionHash = market.derivePayoutDistributionHash(payoutDistribution)
        assert payoutDistributionHash
        childBranchAddress = parentBranch.getChildBranch(payoutDistributionHash)
        assert childBranchAddress
        childBranch = ABIContract(self.chain, ContractTranslator(ContractsFixture.signatures['branch']), childBranchAddress)
        return(childBranch)

    def createBinaryMarket(self, branch, endTime, feePerEthInWei, denominationToken, automatedReporterAddress, topic):
        return self.createCategoricalMarket(branch, 2, endTime, feePerEthInWei, denominationToken, automatedReporterAddress, topic)

    def createCategoricalMarket(self, branch, numOutcomes, endTime, feePerEthInWei, denominationToken, automatedReporterAddress, topic):
        marketCreationFee = self.contracts['MarketFeeCalculator'].getValidityBond(branch.getCurrentReportingWindow()) + self.contracts['MarketFeeCalculator'].getTargetReporterGasCosts()
        marketAddress = self.contracts['MarketCreation'].createCategoricalMarket(branch.address, endTime, numOutcomes, feePerEthInWei, denominationToken.address, automatedReporterAddress, topic, value = marketCreationFee)
        assert marketAddress
        market = ABIContract(self.chain, ContractTranslator(ContractsFixture.signatures['market']), marketAddress)
        return market

    def createScalarMarket(self, branch, endTime, feePerEthInWei, denominationToken, minDisplayPrice, maxDisplayPrice, automatedReporterAddress, topic):
        marketCreationFee = self.contracts['MarketFeeCalculator'].getValidityBond(branch.getCurrentReportingWindow()) + self.contracts['MarketFeeCalculator'].getTargetReporterGasCosts()
        marketAddress = self.contracts['MarketCreation'].createScalarMarket(branch.address, endTime, feePerEthInWei, denominationToken.address, minDisplayPrice, maxDisplayPrice, automatedReporterAddress, topic, value = marketCreationFee)
        assert marketAddress
        market = ABIContract(self.chain, ContractTranslator(ContractsFixture.signatures['market']), marketAddress)
        return market

    def createReasonableBinaryMarket(self, branch, denominationToken):
        return self.createBinaryMarket(
            branch = branch,
            endTime = long(self.chain.head_state.timestamp + timedelta(days=1).total_seconds()),
            feePerEthInWei = 10**16,
            denominationToken = denominationToken,
            automatedReporterAddress = tester.a0,
            topic = 'Sports'.ljust(32, '\x00'))

    def createReasonableCategoricalMarket(self, branch, numOutcomes, denominationToken):
        return self.createCategoricalMarket(
            branch = branch,
            numOutcomes = numOutcomes,
            endTime = long(self.chain.head_state.timestamp + timedelta(days=1).total_seconds()),
            feePerEthInWei = 10**16,
            denominationToken = denominationToken,
            automatedReporterAddress = tester.a0,
            topic = 'Sports'.ljust(32, '\x00'))

    def createReasonableScalarMarket(self, branch, minDisplayPrice, maxDisplayPrice, denominationToken):
        return self.createScalarMarket(
            branch = branch,
            endTime = long(self.chain.head_state.timestamp + timedelta(days=1).total_seconds()),
            feePerEthInWei = 10**16,
            denominationToken = denominationToken,
            minDisplayPrice = minDisplayPrice,
            maxDisplayPrice = maxDisplayPrice,
            automatedReporterAddress = tester.a0,
            topic = 'Sports'.ljust(32, '\x00'))

@fixture(scope="session")
def sessionFixture():
    return ContractsFixture()

@fixture
def contractsFixture(sessionFixture):
    sessionFixture.resetSnapshot()
    return sessionFixture
